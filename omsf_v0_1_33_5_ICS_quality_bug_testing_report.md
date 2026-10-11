# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.5 — ICS Quality Bug & Testing Report

**Release:** v0.1.33.5 (partial shading: geometry and table)
**Date:** 11 October 2026
**Suite:** 392 tests passing (+144 subtests), up from 266
**Static analysis:** pyflakes clean (`custom_components`, `tests`, `tools`)
**Mutation testing:** 107/108 caught + 1 documented equivalent mutant (M71, unchanged since 0.1.33.4); 35 new (M74–M108), first run 32/35, after test fixes 35/35
**Standard applied:** *if I break it, does a test fail?* and *does every
external input have a bound?* Additionally for geometry: *is every shadow
checked against a value that can be calculated by hand?*

---

## 1. Scope

1. **Geometry** (`shading.py`): every obstacle type, the module-field layout
   (portrait/landscape, bypass sections, half-cut halves, strings) and the
   electrical models.
2. **Table format**: parsing, every rejection rule, interpolation, wrap,
   partial ranges, monthly tables, round trip.
3. **Application** to irradiance: exact removal of the direct component,
   parity at factor 1, sub-moment handling, low sun, DNI cap, month.
4. **Home Assistant**: Open-Meteo, local and hybrid mode with the real
   library; parity without a file; repair issues; dialogs; cache;
   diagnostics; upgrade from 0.1.33.4.
5. **The owner's South geometry** against the data-fitted analysis model.
6. **Regression**: all 266 tests of 0.1.33.4 unchanged and passing.

---

## 2. New tests (126)

### 2.1 `test_shading_geometry.py` (60) — analytic shadow edges

| Case | Hand calculation | Checked at |
|---|---|---|
| Vertical wall 5 m east, 5 m high (polygon) | edge at atan(5/5) = 45° | 42° shaded, 48° free; sun on the other side free |
| Box, near face 5 m east, 5 m high | 45° | 44° / 46° |
| Rotated thin box (2 m × 0.2 m) 6 m north | lateral miss when 6·tan(Δaz) > 1 m | 0° shaded, 20° off free |
| Gable building, eave 2 m at 8 m, ridge 4 m at 10 m | ridge governs: atan(4/10) = 21.8° (eave 14.0°) | 20° / 24° |
| Gable building, ridge along the ray | 21.8° | 20° / 24° |
| Cylinder r = 1 m at 10 m, 10 m high | entry face 9 m: atan(10/9) = 48.0°; lateral miss Δaz > 5.7° | 46° / 50°; 94° shaded, 100° free |
| Cone r = 2 m, apex 10 m at 10 m | tan(el) ≤ 1 → 45° | 44° / 46° |
| Frustum r = 2 / 2 (= cylinder r 2) | atan(10/8) = 51.3° | 50° / 52° |
| Concave U-shaped wall with notch above 2 m | even-odd rule: through the notch at 30°, wall at 20° | both |
| Obstacle behind the module / sun behind the panel plane | never shades | ✓ |
| Monthly transparency (birch), constant transparency, two obstacles in series | 0.7 (Jan), 0 (Jul), 0.3 (Apr); 0.4; 0.5 × 0.5 = 0.25 | ✓ |

Electrical model, hand-worked at 800 W/m² direct, 120 W/m² diffuse:

| Case | Expected |
|---|---|
| 1 of 3 sections dark | max(3·120, 2·920) / (3·920) = 0.667 |
| Area model, 1 of 3 dark | (120 + 800·2/3) / 920 |
| Full cells, one half of every section dark | 3·120 / 2760 = 0.130 |
| Half-cut, same shade | 3·520 / 2760 = 0.565 |
| String of 3, one module dark | 6·920 / 9·920 (bypass beats low current) |
| Two strings, one shaded | the other string unaffected |
| Landscape vs portrait | sections are stripes across the short side; halves split the long side |

Validation: 29 malformed geometries, each with the field named in the
message; bad YAML with line number; Python tags refused (safe loader);
oversized file; work budget (sample density reduced, then refused); solids
overlapping the module field refused, neighbouring solids accepted;
round trip geometry → text table → table.

### 2.2 `test_shading_table_apply.py` (35)

- Table: wrap at north, 0/360 without double wrap, partial azimuth range
  (unshaded outside), elevation clamp below / unshaded above, bilinear,
  separators and comments, monthly; 13 rejection rules; row and size limits;
  dispatch by file name.
- Sun position: vectorised NOAA equals the engine's `sun_position`
  (cos Z ±10⁻⁹, azimuth ±10⁻⁶°) and pvlib NREL SPA (elevation < 0.1°,
  azimuth < 0.15°) over 1300 moments of a year.
- Application against **pvlib** clear sky and plane-of-array beam:
  factor 1 → bit-identical; factor 0 → instants equal GTI − beam (±2 W/m²),
  daily removed energy = beam energy ±1 %; factor 0.5 → half; direct and
  diffuse columns untouched; `None` and night preserved; sub-moments below
  1° excluded (exact formula); DNI capped at 1361 W/m² for instants and
  averages (exact); mismatched columns left alone; month selection; stats.

### 2.3 `test_shading_integration.py` (21) — real Home Assistant

The Open-Meteo path runs the **real library** against a synthetic clear-sky
response (pvlib), so shading is tested exactly where it runs.

| Test | Proves |
|---|---|
| without shading keeps the library class | R26 parity |
| shading reduces only shaded hours (+ second refresh) | morning < 0.6 × reference, afternoon identical; counter reset per refresh |
| geometry file for the South array | October: 9:00 reduced, 13:00 identical, day loss 2–12 % |
| orientation mismatch reported | warnings for azimuth and tilt |
| invalid file never stops the forecast | entry loaded, unshaded, repair issue, attribute `error` |
| missing file → issue; fixed → issue cleared | F17 |
| removing the entry removes its issues | no orphans |
| tracking array refused gracefully | L-S4 |
| multi-array: only the array with a file is shaded | per-array mapping |
| cache / changed file read again / unreadable, oversized, binary, directory | F19, bounded read |
| diagnostics: file name only | no path exposure |
| local mode applies the same shading; hybrid gives both paths the tables | R32 |
| config flow: invalid file, tracker, valid geometry (path trimmed), no file → `""` | dialogs |
| options flow: prefilled, rejects missing file, can clear | dialogs |
| entry from 0.1.33.4 without the key | upgrade |

### 2.4 `test_shading_examples.py` (9)

South geometry vs the fitted analysis model at 17 sun positions (±0.07);
unshaded from midday on; shipped table equals the geometry; reference
example with all types valid and monthly; CLI tool output equals the shipped
table and reports invalid files with exit code 2; EN/DE strings complete
with placeholders; `shading.py` free of Home Assistant and file I/O.

### 2.5 `test_options_switch_to_hybrid.py` (1)

Added after 0.1.33.4 (live incident: the owner's switch to Hybrid was not
saved): an existing local entry switched to Hybrid through the full options
dialog saves `hybrid` and serves Open-Meteo days.

---

## 3. Defects found in this pass

See release audit §7: D-1 (broadcasting in the cone/cylinder test), D-2
(float noise at factor 1), D-3 (float noise in tables), D-4 (no overlap
check — found by writing the example), D-5 (two vacuous tests, found by
mutation), D-6 (test-harness daily range), P-1 (two wrong statements of
mine in the design discussion, corrected by the owner).

---

## 4. Mutation record

New mutations M74–M108 (35) target the shading code. First run: 32/35
caught. Escaped:

| ID | Mutation | Why it escaped | Fix |
|---|---|---|---|
| M76 | low-sun guard removed (averages) | the test only checked instants and had no sample between 0° and 1° | `test_quarter_average_ignores_sub_moments_below_one_degree` asserts the exact formula on a quarter whose sub-moments straddle 0° and 1° |
| M77 | DNI cap removed (averages) | the plane irradiance (900 W/m²) limited the result before the cap | test with 5000 W/m² asserting the exact capped removal for instants and averages |
| M80 | array counter not reset per `estimate()` | only one refresh was tested | second refresh asserted |

After the fixes: 35/35 caught.

**Full run on the final code (all 108 mutations, fresh copy each):**
107/108 caught. M71 (OMSF-012: `AttributeError` not caught for retained
data) escapes as documented in 0.1.33.4: the preceding type checks already
reject every input that could raise `AttributeError`, so the mutant is
equivalent. All 73 mutations of earlier releases still apply textually and
are still caught. Results: `tests/mutation/mutation_results_0.1.33.5.json`.

---

## 5. Performance (this container, Python 3.13, under load from a parallel mutation run)

| Operation | Time |
|---|---|
| South geometry → table (15 modules, 2 obstacles) | 0.24 s |
| Reference example (12 modules, 6 obstacles, monthly) | 2.2 s |
| Application to one response (108 days × 96 values) | 29 ms |

Geometry is built once per load in the executor and cached.

---

## 6. Gaps

- **No live run yet** on the owner's Home Assistant 2026.9.4 with the real
  Open-Meteo API and Fusion; the integration tests use a synthetic
  clear-sky response and the Fusion fixtures.
- **Electrical model** verified against hand calculations, not against a
  string I–V simulation (pvlib's single-diode string model was not used);
  the bypass approximation (global MPP over section capabilities) is the
  standard simplified model.
- **South geometry** validated against hourly data Feb–Oct only.
- Home Assistant verified at 2026.2.3 (test harness), as before.
