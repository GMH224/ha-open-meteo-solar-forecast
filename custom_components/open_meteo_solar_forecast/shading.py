"""Partial shading of a PV array by nearby objects (0.1.33.5).

Pure module: no Home Assistant imports, no file or network I/O, no clock.
The HA glue (file reading, caching, repair issues, wiring into the forecast
classes) lives in ``shading_provider.py``.

Two ways to describe shading, per array (= per PV entry or per array of a
multi-array entry):

* **Shading table** - plain text, like the horizon file. One row per sun
  position: ``azimuth  elevation  factor`` (optionally ``month`` first). The
  factor is the share of *direct* light that still produces power at that sun
  position (1 = unshaded, 0 = fully lost).
* **Geometry file** - YAML. Describes the module field (orientation, layout,
  module size, bypass diodes, strings) and the objects around it (boxes,
  gable-roof buildings, planar polygons, cylinders and cones e.g. for trees,
  optionally see-through by month). The geometry is ray-traced once into a
  shading table.

Application (identical for Open-Meteo, local and hybrid data): for every
15-minute value the direct light on the panel plane is derived from the
horizontal direct radiation and the sun position, and only its blocked share
is removed from the plane-of-array irradiance. Diffuse light is never touched.

Coordinate frame of geometry files: metres, x = east, y = north, z = up, any
origin. Angles: compass degrees (0 = north, 90 = east, 180 = south).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# --------------------------------------------------------------------------
# Limits (ICS: every externally controlled collection is bounded)
# --------------------------------------------------------------------------
SHADING_FILE_MAX_BYTES = 256 * 1024
TABLE_MAX_ROWS = 70_000
TABLE_MAX_AZIMUTHS = 721
TABLE_MAX_ELEVATIONS = 181
MAX_OBSTACLES = 64
MAX_POLYGON_VERTICES = 64
MAX_MODULES = 120
MAX_STRINGS = 16
MAX_BYPASS_DIODES = 6
MAX_COORDINATE_M = 1000.0
MAX_SIZE_M = 500.0
# Upper bound for the ray-tracing work (directions x sample points x
# primitive cost). Keeps a pathological file from blocking an executor
# thread for minutes; the sample density is reduced first.
WORK_BUDGET = 6.0e8

GEOMETRY_GRID_STEP_DEG = 2.0
# Reference light used to turn the electrical (bypass-diode) result into a
# direct-light factor. Documented limitation: the bypass effect depends
# slightly on the direct/diffuse ratio; under cloud the direct part is small,
# so the error there is small as well.
REFERENCE_BEAM_W_M2 = 800.0
REFERENCE_DIFFUSE_W_M2 = 120.0

MIN_SUN_ELEVATION_DEG = 1.0  # below: direct light treated as diffuse (as in local_source)
MAX_DNI_W_M2 = 1361.0  # physical bound when deriving DNI from horizontal direct light
POINT_OFFSET_M = 0.01  # sample points sit 1 cm in front of the module surface
SHADE_EPSILON = 1e-6

TABLE_FORMAT = "table"
GEOMETRY_FORMAT = "geometry"
GEOMETRY_SUFFIXES = (".yaml", ".yml")

ELECTRICAL_BYPASS = "bypass"
ELECTRICAL_AREA = "area"
CELL_FULL = "full"
CELL_HALF_CUT = "half_cut"


class ShadingError(ValueError):
    """A shading file is invalid. The message is safe to show to the user."""


# --------------------------------------------------------------------------
# Shading table (the common internal representation)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ShadingTable:
    """Direct-light factor on a regular (azimuth, elevation) grid, per month.

    ``factors`` has shape (12, n_azimuth, n_elevation); months are 1..12 at
    index 0..11. Outside the azimuth range of a non-wrapping table and above
    the highest elevation the factor is 1 (no shading information = no
    shading); below the lowest elevation the lowest row is used.
    """

    azimuths: np.ndarray
    elevations: np.ndarray
    factors: np.ndarray
    source: str
    digest: str
    wraps: bool
    monthly: bool
    summary: dict[str, Any] = field(default_factory=dict)

    def factor(
        self, month: np.ndarray, azimuth_deg: np.ndarray, elevation_deg: np.ndarray
    ) -> np.ndarray:
        """Bilinear interpolation of the factor; vectorised."""
        month = np.asarray(month, dtype=int)
        az = np.mod(np.asarray(azimuth_deg, dtype=float), 360.0)
        el = np.asarray(elevation_deg, dtype=float)
        azs, els = self.azimuths, self.elevations
        fac = self.factors
        if self.wraps:
            # close the circle: append the first column at +360
            azs = np.append(azs, azs[0] + 360.0)
            fac = np.concatenate([fac, fac[:, :1, :]], axis=1)
            az = np.where(az < azs[0], az + 360.0, az)
        inside_az = (az >= azs[0]) & (az <= azs[-1])
        above = el > els[-1]
        el_c = np.clip(el, els[0], els[-1])
        az_c = np.clip(az, azs[0], azs[-1])
        i = np.clip(np.searchsorted(azs, az_c, side="right") - 1, 0, len(azs) - 2) if len(azs) > 1 else np.zeros(az.shape, int)
        j = np.clip(np.searchsorted(els, el_c, side="right") - 1, 0, len(els) - 2) if len(els) > 1 else np.zeros(el.shape, int)
        m = np.clip(month - 1, 0, 11)
        if len(azs) > 1:
            wa = (az_c - azs[i]) / (azs[i + 1] - azs[i])
            i1 = i + 1
        else:
            wa = np.zeros(az.shape)
            i1 = i
        if len(els) > 1:
            we = (el_c - els[j]) / (els[j + 1] - els[j])
            j1 = j + 1
        else:
            we = np.zeros(el.shape)
            j1 = j
        f = (
            fac[m, i, j] * (1 - wa) * (1 - we)
            + fac[m, i1, j] * wa * (1 - we)
            + fac[m, i, j1] * (1 - wa) * we
            + fac[m, i1, j1] * wa * we
        )
        return np.where(inside_az & ~above, np.clip(f, 0.0, 1.0), 1.0)

    def to_text(self) -> str:
        """Serialise as a table file (4 columns when monthly)."""
        lines = [
            "# Open-Meteo Solar Forecast shading table",
            f"# source: {self.source}  digest: {self.digest}",
            "# columns: " + ("month\t" if self.monthly else "") + "azimuth\televation\tfactor",
        ]
        months = range(12) if self.monthly else range(1)
        for mi in months:
            for ai, az in enumerate(self.azimuths):
                for ei, el in enumerate(self.elevations):
                    prefix = f"{mi + 1}\t" if self.monthly else ""
                    lines.append(f"{prefix}{az:g}\t{el:g}\t{self.factors[mi, ai, ei]:.4f}")
        return "\n".join(lines) + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_SPLIT = re.compile(r"[\t,; ]+")


def parse_shading_table(text: str) -> ShadingTable:
    """Parse a shading table file. Raises ShadingError with a clear message."""
    if len(text.encode("utf-8")) > SHADING_FILE_MAX_BYTES:
        raise ShadingError(f"file is larger than {SHADING_FILE_MAX_BYTES} bytes")
    rows: list[list[float]] = []
    width = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in _SPLIT.split(line) if p]
        if width is None:
            width = len(parts)
            if width not in (3, 4):
                raise ShadingError(
                    f"line {number}: expected 3 columns (azimuth, elevation, factor) "
                    f"or 4 (month, azimuth, elevation, factor), found {width}"
                )
        elif len(parts) != width:
            raise ShadingError(f"line {number}: expected {width} columns, found {len(parts)}")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            raise ShadingError(f"line {number}: not a number") from None
        if not all(math.isfinite(v) for v in values):
            raise ShadingError(f"line {number}: non-finite value")
        rows.append(values)
        if len(rows) > TABLE_MAX_ROWS:
            raise ShadingError(f"more than {TABLE_MAX_ROWS} rows")
    if not rows:
        raise ShadingError("no data rows")
    data = np.array(rows, dtype=float)
    monthly = width == 4
    if monthly:
        months = data[:, 0]
        if np.any((months < 1) | (months > 12) | (months != np.round(months))):
            raise ShadingError("month must be an integer 1-12")
        data_m, data = months.astype(int), data[:, 1:]
    az, el, fac = data[:, 0], data[:, 1], data[:, 2]
    if np.any((az < 0) | (az > 360)):
        raise ShadingError("azimuth must be between 0 and 360")
    if np.any((el < 0) | (el > 90)):
        raise ShadingError("elevation must be between 0 and 90")
    if np.any((fac < 0) | (fac > 1)):
        raise ShadingError("factor must be between 0 and 1")
    azs = np.unique(az)
    els = np.unique(el)
    if len(azs) < 2 or len(els) < 2:
        raise ShadingError("need at least 2 different azimuths and 2 different elevations")
    if len(azs) > TABLE_MAX_AZIMUTHS or len(els) > TABLE_MAX_ELEVATIONS:
        raise ShadingError(
            f"grid too large (limit {TABLE_MAX_AZIMUTHS} azimuths x {TABLE_MAX_ELEVATIONS} elevations)"
        )
    factors = np.full((12, len(azs), len(els)), np.nan)
    ai = np.searchsorted(azs, az)
    ei = np.searchsorted(els, el)
    month_list = data_m if monthly else None
    expected = len(azs) * len(els) * (12 if monthly else 1)
    if len(az) != expected:
        raise ShadingError(
            f"not a complete grid: {len(az)} rows for {len(azs)} azimuths x {len(els)} elevations"
            + (" x 12 months" if monthly else "")
        )
    if monthly:
        for idx in range(len(az)):
            mi = month_list[idx] - 1
            if not math.isnan(factors[mi, ai[idx], ei[idx]]):
                raise ShadingError(f"duplicate row: month {mi + 1}, azimuth {az[idx]:g}, elevation {el[idx]:g}")
            factors[mi, ai[idx], ei[idx]] = fac[idx]
    else:
        seen = np.zeros((len(azs), len(els)), bool)
        for idx in range(len(az)):
            if seen[ai[idx], ei[idx]]:
                raise ShadingError(f"duplicate row: azimuth {az[idx]:g}, elevation {el[idx]:g}")
            seen[ai[idx], ei[idx]] = True
            factors[0, ai[idx], ei[idx]] = fac[idx]
        factors[1:] = factors[0]
    if np.isnan(factors).any():
        raise ShadingError("not a complete grid (missing combinations)")
    # Full circle: either both 0 and 360 present (no wrap needed), or the
    # grid is regular and the gap from the last to 360+first equals the step.
    wraps = False
    if not (azs[0] == 0 and azs[-1] == 360):
        steps = np.diff(azs)
        gap = azs[0] + 360.0 - azs[-1]
        if np.allclose(steps, steps[0]) and abs(gap - steps[0]) < 1e-6:
            wraps = True
    return ShadingTable(
        azimuths=azs, elevations=els, factors=factors, source=TABLE_FORMAT,
        digest=_digest(text), wraps=wraps, monthly=monthly,
        summary={"azimuths": int(len(azs)), "elevations": int(len(els)), "monthly": monthly,
                 "min_factor": float(factors.min())},
    )


# --------------------------------------------------------------------------
# Geometry: validation helpers
# --------------------------------------------------------------------------
def _num(obj: Mapping[str, Any], key: str, where: str, lo: float, hi: float,
         default: float | None = None, integer: bool = False) -> float:
    if key not in obj:
        if default is None:
            raise ShadingError(f"{where}.{key}: missing")
        return default
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShadingError(f"{where}.{key}: must be a number")
    if integer and value != int(value):
        raise ShadingError(f"{where}.{key}: must be a whole number")
    if not lo <= value <= hi:
        raise ShadingError(f"{where}.{key}: must be between {lo:g} and {hi:g}")
    return float(value)


def _vec(obj: Mapping[str, Any], key: str, where: str, length: int) -> np.ndarray:
    if key not in obj:
        raise ShadingError(f"{where}.{key}: missing")
    value = obj[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != length:
        raise ShadingError(f"{where}.{key}: must be a list of {length} numbers")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ShadingError(f"{where}.{key}: must be a list of {length} numbers")
        if abs(item) > MAX_COORDINATE_M:
            raise ShadingError(f"{where}.{key}: coordinates must be within +-{MAX_COORDINATE_M:g} m")
        out.append(float(item))
    return np.array(out)


def _choice(obj: Mapping[str, Any], key: str, where: str, options: tuple[str, ...], default: str) -> str:
    value = obj.get(key, default)
    if value not in options:
        raise ShadingError(f"{where}.{key}: must be one of {', '.join(options)}")
    return value


def _only_keys(obj: Any, where: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ShadingError(f"{where}: must be a mapping")
    unknown = sorted(str(k) for k in obj if k not in allowed)
    if unknown:
        raise ShadingError(f"{where}: unknown key(s) {', '.join(unknown)}")
    return obj


def _transparency(obj: Mapping[str, Any], where: str) -> np.ndarray:
    """Share of light passing the object, per month (12 values)."""
    if "transparency" not in obj:
        return np.zeros(12)
    value = obj["transparency"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 12:
        values = list(value)
    else:
        raise ShadingError(f"{where}.transparency: a number 0-1 or a list of 12 monthly numbers")
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
            raise ShadingError(f"{where}.transparency: values must be between 0 and 1")
    return np.array(values * 12 if len(values) == 1 else values, dtype=float)


def _horizontal(az_deg: float) -> np.ndarray:
    a = math.radians(az_deg)
    return np.array([math.sin(a), math.cos(a), 0.0])


# --------------------------------------------------------------------------
# Geometry: obstacles
# --------------------------------------------------------------------------
class _Obstacle:
    kind = "obstacle"
    cost = 8.0
    transparency: np.ndarray

    label = "obstacle"

    def hits(self, points: np.ndarray, sun: np.ndarray) -> np.ndarray:  # (nd, np) bool
        raise NotImplementedError

    def contains(self, points: np.ndarray) -> np.ndarray:  # (np,) bool
        return np.zeros(points.shape[0], dtype=bool)


class _ConvexSolid(_Obstacle):
    """Intersection of half-spaces n.x <= d (box, gable-roof building)."""

    def __init__(self, kind: str, planes: list[tuple[np.ndarray, float]], transparency: np.ndarray):
        self.kind = kind
        self.planes = planes
        self.transparency = transparency
        self.cost = float(len(planes))

    def hits(self, points: np.ndarray, sun: np.ndarray) -> np.ndarray:
        t0 = np.full((sun.shape[0], points.shape[0]), SHADE_EPSILON)
        t1 = np.full_like(t0, np.inf)
        blocked = np.zeros_like(t0, dtype=bool)
        for normal, offset in self.planes:
            num = offset - points @ normal  # (np,) >= 0 inside
            den = sun @ normal  # (nd,)
            with np.errstate(divide="ignore", invalid="ignore"):
                t = num[None, :] / den[:, None]
            pos = den[:, None] > 1e-12
            neg = den[:, None] < -1e-12
            par = ~(pos | neg)
            t1 = np.where(pos, np.minimum(t1, t), t1)
            t0 = np.where(neg, np.maximum(t0, t), t0)
            blocked |= par & (num[None, :] < 0)
        return (t0 < t1) & ~blocked

    def contains(self, points: np.ndarray) -> np.ndarray:
        inside = np.ones(points.shape[0], dtype=bool)
        for normal, offset in self.planes:
            inside &= points @ normal < offset - 1e-9
        return inside


class _Polygon(_Obstacle):
    """Planar polygon (any simple shape, e.g. a wall, a fence, a roof face)."""

    kind = "polygon"

    def __init__(self, vertices: np.ndarray, transparency: np.ndarray, where: str):
        self.transparency = transparency
        centre = vertices.mean(axis=0)
        # Newell normal: robust for concave polygons
        normal = np.zeros(3)
        for i in range(len(vertices)):
            a, b = vertices[i], vertices[(i + 1) % len(vertices)]
            normal += np.array([(a[1] - b[1]) * (a[2] + b[2]),
                                (a[2] - b[2]) * (a[0] + b[0]),
                                (a[0] - b[0]) * (a[1] + b[1])])
        length = np.linalg.norm(normal)
        if length < 1e-6:
            raise ShadingError(f"{where}: polygon has no area")
        normal /= length
        if np.max(np.abs((vertices - centre) @ normal)) > 0.05:
            raise ShadingError(f"{where}: polygon vertices are not in one plane (tolerance 5 cm)")
        self.normal, self.offset = normal, float(normal @ centre)
        # 2-D basis in the plane
        helper = np.array([0.0, 0.0, 1.0]) if abs(normal[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
        e1 = np.cross(normal, helper)
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(normal, e1)
        self.basis = np.stack([e1, e2])
        self.poly2d = (vertices - centre) @ self.basis.T
        self.centre = centre
        self.cost = float(len(vertices) + 3)

    def hits(self, points: np.ndarray, sun: np.ndarray) -> np.ndarray:
        num = self.offset - points @ self.normal
        den = sun @ self.normal
        with np.errstate(divide="ignore", invalid="ignore"):
            t = num[None, :] / den[:, None]
        valid = np.isfinite(t) & (t > SHADE_EPSILON)
        t = np.where(valid, t, 0.0)
        # hit point in plane coordinates
        rel = points[None, :, :] + t[..., None] * sun[:, None, :] - self.centre
        u = rel @ self.basis[0]
        v = rel @ self.basis[1]
        inside = np.zeros(u.shape, dtype=bool)
        xs, ys = self.poly2d[:, 0], self.poly2d[:, 1]
        n = len(xs)
        for i in range(n):  # even-odd rule
            x1, y1, x2, y2 = xs[i], ys[i], xs[(i + 1) % n], ys[(i + 1) % n]
            cond = (y1 > v) != (y2 > v)
            with np.errstate(divide="ignore", invalid="ignore"):
                xcross = x1 + (v - y1) * (x2 - x1) / (y2 - y1)
            inside ^= cond & (u < xcross)
        return valid & inside


class _Revolution(_Obstacle):
    """Vertical cylinder or cone (frustum), e.g. a tree crown or a mast."""

    cost = 12.0

    def __init__(self, kind: str, centre_xy: np.ndarray, bottom: float, top: float,
                 r_bottom: float, r_top: float, transparency: np.ndarray):
        self.kind = kind
        self.c = centre_xy
        self.bottom, self.top = bottom, top
        self.r0, self.r1 = r_bottom, r_top
        self.transparency = transparency

    def hits(self, points: np.ndarray, sun: np.ndarray) -> np.ndarray:
        h = self.top - self.bottom
        g = (self.r1 - self.r0) / h  # radius change per metre of height
        dx = points[:, 0] - self.c[0]
        dy = points[:, 1] - self.c[1]
        pz = points[:, 2]
        sx, sy, sz = sun[:, 0][:, None], sun[:, 1][:, None], sun[:, 2][:, None]
        # radius along the ray: rad(t) = r0 + g*(pz + t*sz - bottom) = a0 + a1*t
        a0 = (self.r0 + g * (pz - self.bottom))[None, :]
        a1 = g * sz
        # f(t) = |d + t s_xy|^2 - rad(t)^2 = A t^2 + B t + C ; inside where f <= 0 and rad >= 0
        A = sx * sx + sy * sy - a1 * a1
        B = 2 * (dx[None, :] * sx + dy[None, :] * sy) - 2 * a0 * a1
        C = (dx * dx + dy * dy)[None, :] - a0 * a0
        A, B, C = np.broadcast_arrays(A, B, C)
        # t range where z within [bottom, top] and t > 0 (sun is above the horizon: sz > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            tz0 = (self.bottom - pz[None, :]) / sz
            tz1 = (self.top - pz[None, :]) / sz
        lo = np.maximum(tz0, SHADE_EPSILON)
        hi = tz1
        disc = B * B - 4 * A * C
        with np.errstate(divide="ignore", invalid="ignore"):
            sq = np.sqrt(np.where(disc >= 0, disc, np.nan))
            r1 = (-B - sq) / (2 * A)
            r2 = (-B + sq) / (2 * A)
            lin = -C / B  # A == 0
        cands = [lo, hi, np.where(np.isfinite(r1), r1, lo), np.where(np.isfinite(r2), r2, lo),
                 np.where(np.isfinite(lin), lin, lo)]
        stack = np.sort(np.stack(cands), axis=0)
        lo, hi = np.broadcast_arrays(lo, hi)
        a0, a1 = np.broadcast_arrays(a0, a1)
        hit = np.zeros(C.shape, dtype=bool)
        for k in range(len(cands) - 1):
            mid = 0.5 * (stack[k] + stack[k + 1])
            ok = (mid > lo) & (mid < hi)
            f = A * mid * mid + B * mid + C
            rad = a0 + a1 * mid
            hit |= ok & (f <= 0) & (rad >= 0)
        return hit & (hi > lo)

    def contains(self, points: np.ndarray) -> np.ndarray:
        z = points[:, 2]
        frac = (z - self.bottom) / (self.top - self.bottom)
        radius = self.r0 + (self.r1 - self.r0) * frac
        dist = np.hypot(points[:, 0] - self.c[0], points[:, 1] - self.c[1])
        return (z > self.bottom) & (z < self.top) & (dist < radius)


def _box(o: Mapping[str, Any], where: str) -> _Obstacle:
    _only_keys(o, where, {"type", "name", "center", "width", "depth", "azimuth", "bottom", "top", "transparency"})
    c = _vec(o, "center", where, 2)
    w = _num(o, "width", where, 0.01, MAX_SIZE_M)
    d = _num(o, "depth", where, 0.01, MAX_SIZE_M)
    az = _num(o, "azimuth", where, 0, 360, default=90.0)
    bottom = _num(o, "bottom", where, -MAX_COORDINATE_M, MAX_COORDINATE_M, default=0.0)
    top = _num(o, "top", where, -MAX_COORDINATE_M, MAX_COORDINATE_M)
    if top <= bottom:
        raise ShadingError(f"{where}: top must be above bottom")
    a = _horizontal(az)
    b = _horizontal(az - 90)
    cc = np.array([c[0], c[1], 0.0])
    planes = [(a, a @ cc + w / 2), (-a, -(a @ cc) + w / 2),
              (b, b @ cc + d / 2), (-b, -(b @ cc) + d / 2),
              (np.array([0, 0, 1.0]), top), (np.array([0, 0, -1.0]), -bottom)]
    return _ConvexSolid("box", planes, _transparency(o, where))


def _gable(o: Mapping[str, Any], where: str) -> _Obstacle:
    _only_keys(o, where, {"type", "name", "center", "ridge_azimuth", "length", "width",
                          "eave_height", "ridge_height", "bottom", "transparency"})
    c = _vec(o, "center", where, 2)
    ridge_az = _num(o, "ridge_azimuth", where, 0, 360)
    length = _num(o, "length", where, 0.1, MAX_SIZE_M)
    width = _num(o, "width", where, 0.1, MAX_SIZE_M)
    eave = _num(o, "eave_height", where, -MAX_COORDINATE_M, MAX_COORDINATE_M)
    ridge = _num(o, "ridge_height", where, -MAX_COORDINATE_M, MAX_COORDINATE_M)
    bottom = _num(o, "bottom", where, -MAX_COORDINATE_M, MAX_COORDINATE_M, default=min(0.0, eave - 3.0))
    if ridge < eave:
        raise ShadingError(f"{where}: ridge_height must not be below eave_height")
    if eave <= bottom:
        raise ShadingError(f"{where}: eave_height must be above bottom")
    along = _horizontal(ridge_az)
    side = _horizontal(ridge_az + 90)
    cc = np.array([c[0], c[1], 0.0])
    half = width / 2
    rise = (ridge - eave) / half  # roof slope (m per m)
    s0 = side @ cc
    # roof planes: z <= ridge - rise*|s - s0|  ->  two half-spaces
    planes = [
        (along, along @ cc + length / 2), (-along, -(along @ cc) + length / 2),
        (side, s0 + half), (-side, -s0 + half),
        (np.array([0, 0, -1.0]), -bottom),
        (np.array([rise * side[0], rise * side[1], 1.0]), ridge + rise * s0),
        (np.array([-rise * side[0], -rise * side[1], 1.0]), ridge - rise * s0),
    ]
    return _ConvexSolid("gable_building", planes, _transparency(o, where))


def _polygon(o: Mapping[str, Any], where: str) -> _Obstacle:
    _only_keys(o, where, {"type", "name", "vertices", "transparency"})
    verts = o.get("vertices")
    if not isinstance(verts, Sequence) or isinstance(verts, (str, bytes)) or not 3 <= len(verts) <= MAX_POLYGON_VERTICES:
        raise ShadingError(f"{where}.vertices: 3 to {MAX_POLYGON_VERTICES} points [x, y, z]")
    pts = np.array([_vec({"v": v}, "v", f"{where}.vertices[{i}]", 3) for i, v in enumerate(verts)])
    return _Polygon(pts, _transparency(o, where), where)


def _revolution(o: Mapping[str, Any], where: str, kind: str) -> _Obstacle:
    keys = {"type", "name", "center", "radius", "bottom", "top", "transparency"}
    if kind == "cone":
        keys |= {"radius_top"}
    _only_keys(o, where, keys)
    c = _vec(o, "center", where, 2)
    r = _num(o, "radius", where, 0.01, MAX_SIZE_M)
    bottom = _num(o, "bottom", where, -MAX_COORDINATE_M, MAX_COORDINATE_M, default=0.0)
    top = _num(o, "top", where, -MAX_COORDINATE_M, MAX_COORDINATE_M)
    if top <= bottom:
        raise ShadingError(f"{where}: top must be above bottom")
    r_top = r if kind == "cylinder" else _num(o, "radius_top", where, 0.0, MAX_SIZE_M, default=0.0)
    return _Revolution(kind, c, bottom, top, r, r_top, _transparency(o, where))


_OBSTACLE_BUILDERS = {
    "box": _box,
    "gable_building": _gable,
    "polygon": _polygon,
    "cylinder": lambda o, w: _revolution(o, w, "cylinder"),
    "cone": lambda o, w: _revolution(o, w, "cone"),
}


# --------------------------------------------------------------------------
# Geometry: the module field
# --------------------------------------------------------------------------
@dataclass
class ArraySpec:
    azimuth: float
    tilt: float
    rows: int
    columns: int
    module_ids: list[str]
    strings: list[list[int]]
    cell_type: str
    diodes: int
    electrical: str
    origin: np.ndarray
    right: np.ndarray
    up: np.ndarray
    normal: np.ndarray
    module_width: float
    module_height: float
    gap: float

    def sample_points(self, n_short: int, n_long: int) -> np.ndarray:
        """Points (modules, diodes, halves=2, n_short*n_long, 3).

        Bypass-diode sections run along the module's long side, so they are
        stripes across the short side. Half-cut modules are split in two
        halves along the long side.
        """
        w, h = self.module_width, self.module_height
        landscape = w >= h
        pts = np.zeros((len(self.module_ids), self.diodes, 2, n_short * n_long, 3))
        for m in range(len(self.module_ids)):
            row, col = divmod(m, self.columns)
            base = self.origin + col * (w + self.gap) * self.right + row * (h + self.gap) * self.up
            for d in range(self.diodes):
                for half in range(2):
                    k = 0
                    for i_s in range(n_short):
                        s = (d + (i_s + 0.5) / n_short) / self.diodes  # 0..1 across short side
                        for i_l in range(n_long):
                            ln = 0.5 * half + 0.5 * (i_l + 0.5) / n_long  # 0..1 along long side
                            x, y = (ln, s) if landscape else (s, ln)
                            pts[m, d, half, k] = base + x * w * self.right + y * h * self.up
                            k += 1
        return pts + POINT_OFFSET_M * self.normal


def _array(o: Any) -> ArraySpec:
    where = "array"
    _only_keys(o, where, {"azimuth", "tilt", "origin", "rows", "columns", "module_width",
                          "module_height", "gap", "bypass_diodes", "cell_type", "strings"})
    az = _num(o, "azimuth", where, 0, 360)
    tilt = _num(o, "tilt", where, 0, 90)
    origin = _vec(o, "origin", where, 3)
    rows = int(_num(o, "rows", where, 1, MAX_MODULES, integer=True))
    cols = int(_num(o, "columns", where, 1, MAX_MODULES, integer=True))
    if rows * cols > MAX_MODULES:
        raise ShadingError(f"array: at most {MAX_MODULES} modules")
    w = _num(o, "module_width", where, 0.1, 5.0)
    h = _num(o, "module_height", where, 0.1, 5.0)
    gap = _num(o, "gap", where, 0.0, 1.0, default=0.02)
    diodes = int(_num(o, "bypass_diodes", where, 1, MAX_BYPASS_DIODES, default=3, integer=True))
    cell = _choice(o, "cell_type", where, (CELL_FULL, CELL_HALF_CUT), CELL_FULL)
    ids = [f"r{r + 1}c{c + 1}" for r in range(rows) for c in range(cols)]
    strings_raw = o.get("strings")
    if strings_raw is None:
        strings = [list(range(len(ids)))]
    else:
        if not isinstance(strings_raw, Sequence) or isinstance(strings_raw, (str, bytes)) or not 1 <= len(strings_raw) <= MAX_STRINGS:
            raise ShadingError(f"array.strings: 1 to {MAX_STRINGS} lists of module ids like r1c1")
        index = {mid: i for i, mid in enumerate(ids)}
        strings, used = [], set()
        for si, s in enumerate(strings_raw):
            if not isinstance(s, Sequence) or isinstance(s, (str, bytes)) or not s:
                raise ShadingError(f"array.strings[{si}]: a non-empty list of module ids")
            members = []
            for mid in s:
                if mid not in index:
                    raise ShadingError(f"array.strings[{si}]: unknown module id {mid!r} (use r<row>c<column>, row 1 = bottom, column 1 = left)")
                if mid in used:
                    raise ShadingError(f"array.strings: module {mid} listed twice")
                used.add(mid)
                members.append(index[mid])
            strings.append(members)
        if len(used) != len(ids):
            missing = [m for m in ids if m not in used]
            raise ShadingError(f"array.strings: every module must be in a string; missing {', '.join(missing[:5])}")
    right = _horizontal(az - 90)
    up_h = _horizontal(az + 180)
    t = math.radians(tilt)
    up = up_h * math.cos(t) + np.array([0, 0, math.sin(t)])
    normal = _horizontal(az) * math.sin(t) + np.array([0, 0, math.cos(t)])
    return ArraySpec(az, tilt, rows, cols, ids, strings, cell, diodes, ELECTRICAL_BYPASS,
                     origin, right, up, normal, w, h, gap)


@dataclass
class Geometry:
    array: ArraySpec
    obstacles: list[_Obstacle]
    electrical: str
    digest: str


def parse_geometry(data: Any, digest: str = "") -> Geometry:
    """Validate a parsed YAML document (any object) into a Geometry."""
    root = _only_keys(data, "geometry", {"version", "electrical_model", "array", "obstacles"})
    version = root.get("version")
    if version != 1:
        raise ShadingError("geometry.version: must be 1")
    electrical = _choice(root, "electrical_model", "geometry", (ELECTRICAL_BYPASS, ELECTRICAL_AREA), ELECTRICAL_BYPASS)
    array = _array(root.get("array"))
    obstacles_raw = root.get("obstacles", [])
    if not isinstance(obstacles_raw, Sequence) or isinstance(obstacles_raw, (str, bytes)):
        raise ShadingError("geometry.obstacles: must be a list")
    if len(obstacles_raw) > MAX_OBSTACLES:
        raise ShadingError(f"geometry.obstacles: at most {MAX_OBSTACLES}")
    obstacles = []
    for i, o in enumerate(obstacles_raw):
        where = f"obstacles[{i}]"
        if not isinstance(o, Mapping):
            raise ShadingError(f"{where}: must be a mapping")
        kind = o.get("type")
        if kind not in _OBSTACLE_BUILDERS:
            raise ShadingError(f"{where}.type: one of {', '.join(_OBSTACLE_BUILDERS)}")
        name = o.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 64):
            raise ShadingError(f"{where}.name: text up to 64 characters")
        obstacle = _OBSTACLE_BUILDERS[kind](o, where)
        obstacle.label = f"{where} ({name})" if name else where
        obstacles.append(obstacle)
    # A solid that contains part of the module field is a description error
    # (it would shade those modules at every sun position).
    corners = array.sample_points(1, 1).reshape(-1, 3)
    for obstacle in obstacles:
        if obstacle.contains(corners).any():
            raise ShadingError(f"{obstacle.label}: overlaps the module field")
    return Geometry(array, obstacles, electrical, digest)


def parse_geometry_yaml(text: str) -> Geometry:
    """Parse and validate a YAML geometry file (safe loader only)."""
    import yaml  # PyYAML ships with Home Assistant

    if len(text.encode("utf-8")) > SHADING_FILE_MAX_BYTES:
        raise ShadingError(f"file is larger than {SHADING_FILE_MAX_BYTES} bytes")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ShadingError(f"not valid YAML{where}") from None
    return parse_geometry(data, _digest(text))


# --------------------------------------------------------------------------
# Geometry -> table
# --------------------------------------------------------------------------
def _sun_vectors(az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    a = np.radians(az_deg)
    e = np.radians(el_deg)
    return np.stack([np.sin(a) * np.cos(e), np.cos(a) * np.cos(e), np.sin(e)], axis=-1)


def electrical_factor(light: np.ndarray, spec: ArraySpec, electrical: str,
                      beam: np.ndarray, diffuse: np.ndarray) -> np.ndarray:
    """Output relative to unshaded for each direction.

    light: (nd, modules, diodes, 2, samples) share of direct light at each
    sample point (0..1). Bypass model: a diode section is limited by its
    darkest cell (full cells) or by the mean of its two halves' darkest
    cells (half-cut); each string runs at the current that maximises
    current x number of sections that can carry it (shaded sections are
    bypassed) - the usual global-maximum-power-point approximation.
    """
    beam = np.asarray(beam, float)
    diffuse = np.asarray(diffuse, float)
    if electrical == ELECTRICAL_AREA:
        mean_light = light.reshape(light.shape[0], -1).mean(axis=1)
        return (diffuse + beam * mean_light) / (diffuse + beam)
    darkest = light.min(axis=-1)  # (nd, modules, diodes, 2)
    if spec.cell_type == CELL_HALF_CUT:
        section = darkest.mean(axis=-1)
    else:
        section = darkest.min(axis=-1)
    total_sections = section.shape[1] * section.shape[2]
    power = np.zeros(section.shape[0])
    for members in spec.strings:
        caps = diffuse[:, None] + beam[:, None] * section[:, members, :].reshape(section.shape[0], -1)
        caps = np.sort(caps, axis=1)
        n = caps.shape[1]
        power += (caps * (n - np.arange(n))[None, :]).max(axis=1)
    return power / (total_sections * (diffuse + beam))


def geometry_table(geom: Geometry, step_deg: float = GEOMETRY_GRID_STEP_DEG) -> ShadingTable:
    """Ray-trace the geometry into a direct-light factor table."""
    spec = geom.array
    azs = np.arange(0.0, 360.0, step_deg)
    els = np.arange(0.0, 90.0 + 1e-9, step_deg)
    AZ, EL = np.meshgrid(azs, els, indexing="ij")
    sun = _sun_vectors(AZ.ravel(), np.maximum(EL.ravel(), 0.5))
    cos_aoi = sun @ spec.normal
    active = cos_aoi > 1e-3
    n_dir = int(active.sum())
    primitive_cost = sum(o.cost for o in geom.obstacles) or 1.0
    for n_short, n_long in ((2, 2), (1, 2), (1, 1)):
        n_points = len(spec.module_ids) * spec.diodes * 2 * n_short * n_long
        if n_dir * n_points * primitive_cost <= WORK_BUDGET:
            break
    else:
        raise ShadingError("geometry too complex (reduce modules, obstacles or polygon corners)")
    pts = spec.sample_points(n_short, n_long)
    shape = pts.shape[:-1]
    flat = pts.reshape(-1, 3)
    monthly = any(np.ptp(o.transparency) > 0 for o in geom.obstacles)
    n_months = 12 if monthly else 1
    k = np.ones((n_months, sun.shape[0]))
    idx = np.where(active)[0]
    chunk = 256
    for start in range(0, len(idx), chunk):
        sel = idx[start:start + chunk]
        s = sun[sel]
        light = np.ones((n_months, len(sel), flat.shape[0]), dtype=np.float32)
        for obstacle in geom.obstacles:
            hit = obstacle.hits(flat, s)
            if not hit.any():
                continue
            tr = obstacle.transparency if monthly else obstacle.transparency[:1]
            for m in range(n_months):
                light[m] = np.where(hit, light[m] * tr[m], light[m])
        beam = REFERENCE_BEAM_W_M2 * cos_aoi[sel]
        diffuse = np.full(len(sel), REFERENCE_DIFFUSE_W_M2)
        for m in range(n_months):
            lm = light[m].reshape((len(sel),) + shape)
            out = electrical_factor(lm, spec, geom.electrical, beam, diffuse)
            k[m, sel] = np.clip((out * (beam + diffuse) - diffuse) / beam, 0.0, 1.0)
    k = np.where(k > 1 - 1e-9, 1.0, np.where(k < 1e-9, 0.0, k))  # exact 0 / 1, no float noise
    factors = k.reshape(n_months, len(azs), len(els))
    if not monthly:
        factors = np.repeat(factors, 12, axis=0)
    shaded_dirs = float((factors[0][active.reshape(len(azs), len(els))] < 0.99).mean()) if n_dir else 0.0
    return ShadingTable(
        azimuths=azs, elevations=els, factors=factors, source=GEOMETRY_FORMAT,
        digest=geom.digest, wraps=True, monthly=monthly,
        summary={"modules": len(spec.module_ids), "strings": len(spec.strings),
                 "obstacles": len(geom.obstacles), "electrical_model": geom.electrical,
                 "cell_type": spec.cell_type, "monthly": monthly,
                 "sample_points_per_module": spec.diodes * 2 * n_short * n_long,
                 "array_azimuth": spec.azimuth, "array_tilt": spec.tilt,
                 "share_of_sun_positions_shaded": round(shaded_dirs, 3),
                 "min_factor": float(factors.min())},
    )


def load_shading_text(text: str, filename: str) -> ShadingTable:
    """Dispatch by file name: .yaml/.yml = geometry, anything else = table."""
    if filename.lower().endswith(GEOMETRY_SUFFIXES):
        return geometry_table(parse_geometry_yaml(text))
    return parse_shading_table(text)


# --------------------------------------------------------------------------
# Solar position (vectorised NOAA; same formulas as local_source.sun_position)
# --------------------------------------------------------------------------
def solar_position(unix_s: np.ndarray, latitude: float, longitude: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (cos_zenith, azimuth_deg, elevation_deg) for UTC unix seconds."""
    ts = np.asarray(unix_s, dtype=float)
    t = (ts / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    gml = np.radians(np.mod(280.46646 + t * (36000.76983 + t * 0.0003032), 360))
    gma = np.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    eqc = (np.sin(gma) * (1.914602 - t * (0.004817 + 0.000014 * t))
           + np.sin(2 * gma) * (0.019993 - 0.000101 * t) + np.sin(3 * gma) * 0.000289)
    true_long = np.degrees(gml) + eqc
    omega = np.radians(125.04 - 1934.136 * t)
    app_long = np.radians(true_long - 0.00569 - 0.00478 * np.sin(omega))
    mean_obliq = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliq = np.radians(mean_obliq + 0.00256 * np.cos(omega))
    decl = np.arcsin(np.sin(obliq) * np.sin(app_long))
    vy = np.tan(obliq / 2) ** 2
    eot = 4 * np.degrees(vy * np.sin(2 * gml) - 2 * ecc * np.sin(gma)
                         + 4 * ecc * vy * np.sin(gma) * np.cos(2 * gml)
                         - 0.5 * vy * vy * np.sin(4 * gml) - 1.25 * ecc * ecc * np.sin(2 * gma))
    minutes = np.mod(ts, 86400.0) / 60.0
    tst = np.mod(minutes + eot + 4 * longitude, 1440)
    ha = np.radians(tst / 4 - 180)
    lat = math.radians(latitude)
    cz = np.clip(math.sin(lat) * np.sin(decl) + math.cos(lat) * np.cos(decl) * np.cos(ha), -1, 1)
    zen = np.arccos(cz)
    sz = np.sin(zen)
    with np.errstate(divide="ignore", invalid="ignore"):
        caz = np.clip((math.sin(lat) * cz - np.sin(decl)) / (math.cos(lat) * sz), -1, 1)
    az = np.degrees(np.arccos(caz))
    az = np.where(ha > 0, np.mod(az + 180, 360), np.mod(540 - az, 360))
    az = np.where(sz < 1e-9, 0.0, az)
    return cz, az, 90.0 - np.degrees(zen)


def _months_utc(unix_s: np.ndarray) -> np.ndarray:
    return np.asarray(unix_s, dtype="int64").astype("datetime64[s]").astype("datetime64[M]").astype(int) % 12 + 1


def _cos_aoi(cz: np.ndarray, az_deg: np.ndarray, tilt_deg: float, panel_az_deg: float) -> np.ndarray:
    sz = np.sqrt(np.clip(1 - cz * cz, 0, 1))
    t = math.radians(tilt_deg)
    return cz * math.cos(t) + sz * math.sin(t) * np.cos(np.radians(az_deg - panel_az_deg))


# --------------------------------------------------------------------------
# Application to an Open-Meteo-shaped response
# --------------------------------------------------------------------------
@dataclass
class ShadeStats:
    values: int = 0
    shaded_values: int = 0
    removed_wh_m2: float = 0.0
    unshaded_wh_m2: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        share = self.removed_wh_m2 / self.unshaded_wh_m2 if self.unshaded_wh_m2 > 0 else 0.0
        return {"values": self.values, "shaded_values": self.shaded_values,
                "removed_share": round(share, 4)}


def shade_payload(payload: dict[str, Any], table: ShadingTable, latitude: float,
                  longitude: float, tilt_deg: float, panel_azimuth_deg: float) -> ShadeStats:
    """Remove the blocked direct light from the response's plane irradiance.

    Works in place on ``payload["minutely_15"]``. Average values labelled t
    cover [t-15 min, t); instant values are at t (Open-Meteo convention, the
    same the library uses). Only ``global_tilted_irradiance`` and its
    ``_instant`` are changed; ``direct_radiation`` / ``diffuse_radiation``
    stay unshaded on purpose (they describe the sky, and the library's own
    horizon handling reads them).
    """
    stats = ShadeStats()
    m15 = payload.get("minutely_15")
    if not isinstance(m15, dict):
        return stats
    times = np.asarray(m15.get("time", []), dtype=float)
    n = len(times)
    if n == 0:
        return stats

    def column(name: str) -> np.ndarray:
        raw = m15.get(name)
        if not isinstance(raw, list) or len(raw) != n:
            return np.full(n, np.nan)
        return np.array([np.nan if v is None else float(v) for v in raw])

    gti = column("global_tilted_irradiance")
    gti_i = column("global_tilted_irradiance_instant")
    dr = column("direct_radiation")
    dr_i = column("direct_radiation_instant")

    # averages: three sub-moments inside the interval, DNI assumed constant
    offsets = np.array([-12.5, -7.5, -2.5]) * 60.0
    moments = times[:, None] + offsets[None, :]
    cz, az, el = solar_position(moments.ravel(), latitude, longitude)
    cz, az, el = cz.reshape(n, 3), az.reshape(n, 3), el.reshape(n, 3)
    up = el > MIN_SUN_ELEVATION_DEG
    caoi = np.clip(_cos_aoi(cz, az, tilt_deg, panel_azimuth_deg), 0, None)
    month = _months_utc(moments.ravel()).reshape(n, 3)
    k = table.factor(month, az, el)
    k = np.where(k > 1 - 1e-9, 1.0, k)  # unshaded must be exactly unshaded
    mean_cz = np.where(up, cz, 0).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dni = np.where(mean_cz > 1e-6, np.clip(dr, 0, None) / mean_cz, 0.0)
    dni = np.minimum(dni, MAX_DNI_W_M2)
    blocked = dni * np.where(up, caoi * (1 - k), 0).mean(axis=1)
    ok = np.isfinite(gti) & np.isfinite(blocked)
    blocked = np.where(ok, np.minimum(blocked, np.clip(gti, 0, None)), 0.0)
    new_gti = np.where(ok, np.clip(gti - blocked, 0, None), gti)

    # instants
    cz_i, az_i, el_i = solar_position(times, latitude, longitude)
    up_i = el_i > MIN_SUN_ELEVATION_DEG
    caoi_i = np.clip(_cos_aoi(cz_i, az_i, tilt_deg, panel_azimuth_deg), 0, None)
    k_i = table.factor(_months_utc(times), az_i, el_i)
    k_i = np.where(k_i > 1 - 1e-9, 1.0, k_i)
    with np.errstate(divide="ignore", invalid="ignore"):
        dni_i = np.where(up_i & (cz_i > 1e-6), np.clip(dr_i, 0, None) / cz_i, 0.0)
    dni_i = np.minimum(dni_i, MAX_DNI_W_M2)
    blocked_i = dni_i * caoi_i * (1 - k_i)
    ok_i = np.isfinite(gti_i) & np.isfinite(blocked_i)
    blocked_i = np.where(ok_i, np.minimum(blocked_i, np.clip(gti_i, 0, None)), 0.0)
    new_gti_i = np.where(ok_i, np.clip(gti_i - blocked_i, 0, None), gti_i)

    def write(name: str, new: np.ndarray, changed: np.ndarray) -> None:
        original = m15.get(name)
        if not isinstance(original, list) or len(original) != n:
            return
        # untouched values keep their original object (exact parity)
        m15[name] = [float(new[i]) if changed[i] else original[i] for i in range(n)]

    write("global_tilted_irradiance", new_gti, ok & (blocked > 0))
    write("global_tilted_irradiance_instant", new_gti_i, ok_i & (blocked_i > 0))
    stats.values = int(ok.sum())
    stats.shaded_values = int((blocked > 0.5).sum())
    stats.removed_wh_m2 = float(blocked[ok].sum() / 4)
    stats.unshaded_wh_m2 = float(np.clip(gti[ok], 0, None).sum() / 4)
    return stats
