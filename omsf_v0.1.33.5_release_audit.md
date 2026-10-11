# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.5 — Release Audit (ICS)

**Release type:** feature — partial shading per array (geometry or table).
**Owner decisions (11 Oct 2026):** support both a shading table and a 3-D
geometry file; YAML; bypass-diode model as default with the area model as an
option; trees with seasonal transparency included; design confirmed before
coding.
**Invariants:** entries without a shading file behave exactly as in
0.1.33.4 (unmodified library class, bit-identical values). The horizon file
and its partial-shading switch are unchanged.
**Verification:** see §6 and `omsf_v0_1_33_5_ICS_quality_bug_testing_report.md`.

---

## 1. Why

The owner observed that the forecast for the South array is too high on
sunny mornings and that "only the right part of the South array gets shaded
and not even in the whole height". A horizon file cannot express that: it
switches the whole array between full sun and diffuse-only. The owner asked
for a general, flexible mechanism ("geometries can be included or defined
similar to the horizon file").

## 2. Evidence: the South array (analysis of 11 Oct 2026)

**Data.** Owner's Home Assistant statistics, 20 Jan – 11 Oct 2026, hourly:
string 1 (West, 4 kWp) and string 2 (South, 6 kWp) voltage × current, DC
energy counters of both inverters (5K = East, 7 kWp), AC power, battery,
grid. May has no string values (inverter total only) and was excluded for
West/South. The exported statistics are stamped at the **end** of the hour
(verified: a one-hour offset in every month, independent of DST); corrected
before any analysis.

**Method.** Clear-sky reference per array (Ineichen clear sky, Hay–Davies,
Martin–Ruiz reflection losses, monthly temperature climatology); clear days
selected by day clearness (> 0.7–0.75); actual ÷ reference per sun-position
bin. Shadow casting was then modelled in 3-D: the tall north–south wing as a
gable-roof solid east of the array, the chimney as a box, the South field as
15 landscape modules (3 × 5) with three bypass diodes each in one string
(string voltage ≈ 520 V confirms 15 in series). Free parameters fitted by
grid search to 561 clear-day morning hours: wing width, gap between array
and valley corner, chimney on/off, cell type.

**Result.**

| | Weighted RMSE of hourly ratio |
|---|---|
| No shading | 0.200 |
| Fitted 3-D model (wing 11 m wide → ridge 5.5 m above the shared eave, gap 1.0 m, chimney) | **0.141** |
| Noise floor (unshaded afternoon hours, same days) | 0.187 |

The fitted model explains the morning losses down to the noise of hourly
data; binned medians agree (e.g. sun 110°/12–24°: measured 0.41–0.52, model
0.35–0.59). Full-cell vs half-cut cannot be distinguished by the data.
Extrapolated with the model to the whole year (clear-sky days): loss 1.8 %
(June) to 6.5 % (December/January) of South energy, 3.4 % per year;
shaded from sunrise / ≈ 8:00 until ≈ 10:00–10:30 local time.

The fitted geometry is shipped as `examples/shading/south_geometry.yaml`;
`test_south_geometry_reproduces_the_fitted_analysis_model` pins it to the
analysis model (17 sun positions, ±0.07).

**Other findings of the same analysis (settings, not code).** Orientations
are correct (no azimuth/tilt bias for 130–250° sun azimuth). Morning damping
1.0 on West removed 38 % of its real clear-morning output; evening damping
0.9 on East likewise removed real output. Efficiency factors relative to the
library's model: West ≈ 0.83, South ≈ 0.87, East ≈ 0.81 against an ideal
clear sky; the week of 6–11 Oct with the actual Fusion forecast pointed
higher. Recommended starting values were given to the owner (West 0.82,
South 0.89, East 0.84, damping 0) with ±5 % uncertainty, to be refined from
recorded forecasts.

## 3. Changes

| Area | Change | Files |
|---|---|---|
| Shading engine (pure) | table parser and interpolation; YAML geometry with strict validation; ray tracing (box, gable building, polygon, cylinder, cone; monthly transparency); bypass-diode/string and area models; vectorised NOAA sun position; application to a response | `shading.py` (new) |
| HA glue | bounded file read, cache (32 entries, size + mtime), repair issues, `ShadingMixin`, `ShadedOpenMeteoSolarForecast` | `shading_provider.py` (new) |
| Wiring | load per array at setup (never raises); coordinator chooses the shaded class only with ≥ 1 valid table; local class applies the same step | `__init__.py`, `coordinator.py`, `local_provider.py` |
| Configuration | per-array option `shading_filepath` (empty = none); validated in setup and options dialog; tracking + file refused | `config_flow.py`, `const.py` |
| Visibility | `forecast_source.shading`; diagnostics `shading` (state, file name, digest, summary, warnings, removed share per refresh); repair issue `invalid_shading_file` | `sensor.py`, `diagnostics.py`, strings |
| Translations | EN/DE for field, help, errors, issue | `strings.json`, `translations/*.json` |
| Examples and tool | reference example (all types), South geometry and table; `tools/shading_table.py` | `examples/shading/`, `tools/` |

## 4. Behaviour and parity

- **No shading file:** `type(coordinator.forecast) is OpenMeteoSolarForecast`
  (test); values unchanged. A table factor of 1 leaves each value object
  untouched (bit-identical, test + M108).
- **Options dictionary:** saving the options dialog now stores
  `shading_filepath: ""`. This changes the configuration fingerprint once,
  so the retained forecast is discarded once on that reload (one fresh
  fetch). No other effect.
- **Horizon file + shading file together:** the library applies the horizon
  after our step; when the sun is behind the horizon it replaces the plane
  irradiance by diffuse light anyway, so the two never double-count.
- **Snow:** the library's snow factor applies to the already-shaded value.

## 5. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Untrusted file content (admin-supplied path, like the horizon file) | size ≤ 256 KiB; table ≤ 70 000 rows, ≤ 721 × 181 grid; geometry ≤ 64 obstacles, ≤ 64 polygon corners, ≤ 120 modules, ≤ 16 strings, coordinates ±1000 m, sizes ≤ 500 m; every value finite and range-checked; unknown keys refused |
| Code execution via YAML | `yaml.safe_load` only; Python tags refused (test) |
| CPU exhaustion | work budget 6·10⁸ point × direction × primitive operations: sample density reduced first, then refused ("too complex"); runs in the executor once per load; cached |
| Event loop | file I/O, ray tracing and the per-refresh application run in the executor |
| Fail-safe | a faulty file never stops the forecast: the array is forecast **unshaded** (over-forecasts a shaded array — the pre-0.1.33.5 behaviour) and the fault is visible (repair issue, attribute, diagnostics, log). Chosen over "unavailable" because the shading is a refinement, not a safety input |
| Information exposure | diagnostics show the file name only; the repair issue (admin-only) shows the path; error text passes `sanitize_error` |
| Network | none added |
| Supply chain | no new dependency (numpy and PyYAML ship with Home Assistant) |
| Description errors | solids overlapping the module field, non-planar polygons and inconsistent strings are refused; geometry azimuth/tilt deviating > 3° from the entry is reported as a warning |

Performance (this container, Python 3.13): South geometry 0.4 s to build;
application to one response (108 days × 96 values) 29 ms.

## 6. Verification

392 tests passing (+144 subtests), pyflakes clean. Mutation record: 107/108
caught on the final code (M71 documented equivalent); the 35 new mutations
M74–M108 all caught after three tests were strengthened (test report §4).

## 7. Defects in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | Cone/cylinder ray test crashed for more than one direction (array broadcasting) | first test run | `np.broadcast_arrays`; covered by all revolution tests |
| D-2 | Factor 1 still changed values by ~10⁻¹⁶ (parity) | `test_factor_one_leaves_the_payload_unchanged` | snap k ≥ 1−10⁻⁹ to 1; untouched values keep their object; M108 |
| D-3 | Geometry tables carried float noise (0.9999999) | `test_south_geometry_is_unshaded_from_midday_on` | snap to exact 0 / 1 |
| D-4 | The first version of the reference example placed the chimney on the modules; nothing complained | writing the example | new check: solids overlapping the module field are refused (`test_solid_overlapping_*`, M99) |
| D-5 | Two tests passed vacuously: low-sun guard (no sample between 0° and 1°) and DNI cap (plane irradiance limited first) | mutations M76, M77 escaped | tests now pick sun positions explicitly and assert the exact capped value |
| D-6 | Test harness: synthetic Open-Meteo response lacked the last day's sunrise | first integration run (library KeyError) | one more daily entry |
| P-1 | Process: in the discussion I first recommended a horizon file for South (all-or-nothing) and then claimed Open-Meteo delivers only combined plane irradiance | owner corrected both | withdrawn; the design applies shading to the direct component Open-Meteo does deliver, per array |

## 8. Gaps and limitations

- **Live acceptance pending:** not yet run on the owner's Home Assistant
  2026.9.4; verified on 2026.2.3 (test harness).
- **South geometry:** fitted to hourly data Feb–Oct; November–January is the
  model's extrapolation. Recheck in January with recorded data.
- L-S1 … L-S6 (architecture §0c): fixed electrical reference ratio; diffuse
  and circumsolar shade and reflections not modelled; one MPP tracker per
  string assumed; no tracking arrays; ≈ ±5 min edge resolution.
- Cloudy-day over-forecasting (owner's original observation) is **not**
  addressed by this release; the analysis attributes it mainly to the weather
  forecast (6–11 Oct: all three arrays equally low on overcast days). Next
  step: evaluate recorded forecasts against production (TimescaleDB).

## 9. Owner actions

1. Upload, release `0.1.33.5`, HACS redownload, restart (no zip asset).
2. Copy `examples/shading/south_geometry.yaml` to e.g.
   `/config/shading/south_geometry.yaml`.
3. South entry → Configure → (pages 1–2 unchanged) → array page: **Shading
   file** = that path; **Use horizon** as before. Leave West and East empty.
4. Check: no repair issue; `forecast_source` attribute `shading` shows
   `geometry` for South; diagnostics `shading[0].last_local.removed_share`
   ≈ 0.02–0.07 on sunny October days.
5. Independently of the code: apply the efficiency and damping settings of
   §2 if desired.
