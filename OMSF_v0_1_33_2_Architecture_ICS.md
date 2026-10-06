# Open-Meteo Solar Forecast (GMH224 fork) — v0.1.33.2 Architecture (ICS)

**Feature:** local weather source — PV forecast from Home Assistant entities
(SwissWeather Fusion ≥ 0.3.3) instead of the Open-Meteo API.
**Date:** 6 October 2026
**Companion documents:**
[`omsf_v0.1.33.2_release_audit.md`](omsf_v0.1.33.2_release_audit.md) (what changed, defects, safety analysis),
[`omsf_v0_1_33_2_ICS_quality_bug_testing_report.md`](omsf_v0_1_33_2_ICS_quality_bug_testing_report.md) (tests, mutation record, gaps),
[`DEVELOPER.md`](DEVELOPER.md) (module map, how to run tests).

---

## 0. How to read this

§1 is the summary. §2 lists the requirements and where each is met and
tested. §3–§5 are the design: data flow, the input contract, and the
algorithms with their physical justification. §6 is the failure-mode table —
the section an operator needs. §7 covers ICS concerns (network egress,
trust boundary, resources). §8–§11 are configuration, limitations, risks and
open questions.

Anything not verified is marked **(unverified)**; §13 of the test report
lists what could not be tested without a live installation.

---

## 1. Executive summary

The integration can now compute the PV forecast from **local** weather data:
location-level hourly GHI/DNI/DHI from SwissWeather Fusion's
`sensor.…_solar_irradiance_ghi_hour_average` (attribute `hourly_forecast`)
and the hourly temperature forecast of a `weather.*` entity. The upstream
library `open_meteo_solar_forecast==0.1.32` is **not forked**: a subclass
replaces only its single network method and answers it with a synthesised
Open-Meteo response. All PV physics (cell temperature, horizon / partial
shading, snow, damping, inverter clamping, multi-array) therefore run
unchanged and the two sources stay directly comparable.

What the solar layer adds on top of Fusion's location-level data:

1. **Temporal downscaling** hour → 15 min with exact conservation of hourly
   GHI and DHI energy.
2. **Transposition** onto each array's plane (Hay–Davies), including
   trackers. Validated against pvlib to 1 × 10⁻⁶ relative.
3. **History** of elapsed hours (Fusion publishes only from the current hour
   on), so "energy today" and the Energy dashboard stay correct.

Operational stance: **fail-safe and offline by default.** In local mode the
integration makes no internet request. If local data is missing, invalid or
stale, the last good forecast is retained and the new diagnostic sensor
`sensor.<name>_forecast_source` reads `retained`. Falling back to Open-Meteo
is an explicit opt-in.

Open-Meteo mode (the default, and every existing installation after upgrade)
is functionally unchanged: same library arguments, same 30-minute interval,
same retained-forecast fingerprint, no migration.

---

## 2. Requirements and traceability

| ID | Requirement | Source | Implementation | Verified by (tests) |
|---|---|---|---|---|
| R1 | A local HA weather source can be configured in addition to the Open-Meteo models | owner | `config_flow.py` step `local`; option `weather_source` | `test_config_flow_local_auto_discovers_the_fusion_sensors`, `test_options_flow_local_step_is_prefilled_with_the_stored_entities` |
| R2 | When configured for `weather.swissweather_fusion_…`, the forecast uses HA data, not an Open-Meteo model | owner | `coordinator._async_fetch_estimate`, `local_provider.LocalOpenMeteoSolarForecast` | `test_local_mode_produces_a_forecast_without_contacting_open_meteo` |
| R3 | Local refresh every 10 minutes (Fusion updates every 5 min) | owner | `LOCAL_UPDATE_MINUTES = 10` | `test_local_mode_produces_…` (interval assert); mutation M24 |
| R4 | Version 0.1.33.2 | owner | `manifest.json` | review |
| R5 | ICS/OT treatment: full documentation, audit, test suite | owner | this document, audit, test report, `tests/` | test report |
| R6 | Open-Meteo mode must behave exactly as 0.1.33.1 | derived (safety invariant) | branch only on `weather_source == "local"` | `test_open_meteo_mode_is_unchanged_…`, `…_passes_the_same_library_arguments_as_before`, `test_an_existing_entry_keeps_its_retained_forecast_fingerprint`, `test_open_meteo_mode_never_masks_day_sensors`; mutation M25 |
| R7 | No internet egress in local mode | derived (ICS) | adapter overrides the library's only network method | network guard in every local test (`_request` raises) |
| R8 | Fail-safe on bad local data; fallback only on explicit opt-in | derived (ICS) | `LocalDataError` → retained; `local_fallback_open_meteo` default false | `test_stale_local_data_…`, `test_fallback_to_open_meteo_only_when_explicitly_enabled`, `test_fallback_is_off_when_the_option_is_absent`; mutations M22, M23, M26 |
| R9 | Consume Fusion v0.3.3's series without shifting it again | Fusion v0.3.3 contract | §4; label handling in `build_open_meteo_payload` | `test_an_average_is_labelled_at_the_end_of_its_quarter`, `test_instant_values_at_hour_starts_reproduce_the_published_instants`; mutations M01, M09 |
| R10 | Panel geometry (tilt, azimuth, GTI) owned by the solar layer, per array | Fusion v0.3.3 design | `transpose_hay_davies` per array from the library's request parameters | `test_hay_davies_transposition_matches_pvlib`, `test_multi_array_east_west_uses_one_snapshot_and_two_geometries` |
| R11 | Sensors must not report a made-up value for days without local data | derived (D-05) | `local_complete_dates` + sensor masking | `test_days_beyond_the_local_horizon_are_unknown_not_zero`; mutations M31, M32 |
| R12 | Operator can see which source produced the forecast | derived (ICS) | `sensor.<name>_forecast_source`, diagnostics `source` block | `test_stale_local_data_later_serves_the_retained_forecast_and_says_so`, `test_diagnostics_report_the_source_and_redact_coordinates` |

---

## 3. Data flow

```
 SwissWeather Fusion (>= 0.3.3)                 this integration (local mode, every 10 min)
 ───────────────────────────────                ────────────────────────────────────────────────
 sensor.*_solar_irradiance_ghi_hour_average ─┐
   attr hourly_forecast (UTC hours,          │   LocalWeatherReader.async_snapshot()
   ghi/dni/dhi avg, *_instant, sources)      ├──►  1 parse + validate (local_source.parse_*)
 weather.swissweather_fusion_*               │     2 freshness check (series_freshness_problem)
   service weather.get_forecasts (hourly) ───┤     3 merge with history store (elapsed hours)
 sensor.*_snow_depth (optional, metres) ─────┘     4 freeze → LocalSnapshot
                                                          │
                                                          ▼
                                     LocalOpenMeteoSolarForecast.estimate()   (upstream library)
                                       per array: _request(params: lat, lon, tilt, azimuth)
                                          └─► build_open_meteo_payload()  [executor thread]
                                                downscale 1 h → 15 min, transpose to plane,
                                                daily sunrise/sunset, Open-Meteo field names
                                       library PV physics → Estimate (watts, wh_period, wh_days)
                                                          │
                                                          ▼
                     coordinator: complete_local_dates → sensors (unknown for uncovered days)
                     retained-forecast store, forecast_source sensor, diagnostics
```

Failure at any step before the library → `LocalDataError` → §6.

---

## 4. Input contract (SwissWeather Fusion v0.3.3)

Read **live** from the state machine. Fusion excludes the series from the
recorder (`_unrecorded_attributes`), so history cannot be used — which is why
§5.6 keeps its own.

### 4.1 Irradiance series — `hourly_forecast`

| Field | Meaning (per Fusion v0.3.3 audit §1) | Validation here |
|---|---|---|
| `period_start` | hour start, UTC ISO-8601 | must parse, must be timezone-aware, must be on a full hour |
| `period_end` | `period_start` + 1 h | if present, must equal start + 1 h |
| `ghi`, `dni`, `dhi` | averages over `[period_start, period_end)`, W/m² | finite numbers, not bool; 0 ≤ v ≤ 1500; `dhi ≤ ghi + 5`; all three or none |
| `ghi_instant`, `dni_instant`, `dhi_instant` | values at `period_start` | same rules, independently |
| `sources` | models contributing (3 near-term → 1 beyond ~48 h) | carried into diagnostics; not used in the computation |

An invalid triple is dropped and the reason recorded (bounded to 50
entries); an invalid timestamp drops the whole entry. A duplicate hour is
reported and the later entry wins. Fusion's end-of-hour Open-Meteo labelling
is **already converted** — this integration does not shift again (R9).

### 4.2 Temperature — `weather.get_forecasts` (type `hourly`)

`datetime` + `temperature` per hour, in the entity's `temperature_unit`
(°C, °F or K converted to °C); plausibility −60…60 °C. If the service call
fails, the entity's current `temperature` is used for all quarters and the
failure is reported. If neither exists, the refresh fails (§6).

### 4.3 Snow depth (optional)

State in m (cm, mm, in, ft converted), 0…20 m. Only consumed when the
array option *maximum snow cover depth* is > 0 (library behaviour).

### 4.4 SRF global irradiance (optional, comparison only)

Shown in diagnostics next to the ICON current-hour GHI. Never used in the
computation: SRF's averaging basis and labelling are undocumented (Fusion
v0.3.3 audit §2).

### 4.5 Auto-discovery

When the irradiance / snow sensor fields are left empty, they are found in
the entity registry by Fusion's config entry and unique-ID suffix
(`_weather` → `_solar_ghi`, `_snow_depth`, `_srf_irradiance`). The resolved
entity IDs are **stored explicitly**, so runtime never depends on discovery.

---

## 5. Algorithms

All in `local_source.py` — pure Python, no Home Assistant import, no clock
read (`now` is always passed in). Both properties are enforced by tests.

### 5.1 Solar position

NOAA / Meeus formulation (geometric, no refraction). Verified against NREL
SPA (pvlib, `nrel_numpy`) over a full year in 7-hour steps: max zenith error
**0.012°**, max azimuth error **0.015°** (test threshold 0.02° / 0.03°).
Extraterrestrial normal irradiance E0 = 1361 · (1 + 0.033 cos(2π·doy/365)).

### 5.2 Sunrise / sunset (for the library's damping)

Bisection on elevation = −0.833° around the day's elevation maximum. Agrees
with SPA within **1 s** (test threshold 60 s). Polar night/day keep
`sunset > sunrise`, because the library divides by `sunset − sunrise`.

### 5.3 Downscaling hour averages → 15-minute averages

Per hour, clearness index and diffuse fraction:

    kt = GHI_avg / mean(E0 · max(cos Z, 0))        kd = DHI_avg / GHI_avg

The mean runs over the **same 20 sample instants** (3-minute midpoints) that
the four quarters use. Each quarter is then

    GHI_q = kt · mean_q(E0 · cos Z),   DHI_q = kd · GHI_q,   beam_h,q = GHI_q − DHI_q

so the mean of the four quarters reproduces the hourly GHI and DHI
**exactly** (to the 0.01 W/m² output rounding). Shaping by E0·cos Z gives
the physically correct ramp at sunrise/sunset instead of
linear-interpolation artefacts.

*Why GHI and DHI are the anchors, not DNI:* the horizontal energy balance is
the quantity that is conserved; the hour-average DNI multiplied by a mean
cos Z is not equal to the mean of DNI·cos Z. The published DNI is used as a
consistency diagnostic (`dni_consistency_mad_w_m2`).

`kt` is clamped to 1.0 (impossible for hour averages; occurs only in the
sunrise/sunset hour when the model's horizon geometry differs). Each clamp
is counted (`kt_clamped_hours`).

**Quantified downscaling error** (end to end, through the library, against a
1-minute pvlib computation with a clear and a 50 %-cloud day): daily energy
within **0.2 %** (30° south), **0.5 %** (30° east), **1.6 %** (45° west).
The residual comes from holding `kt` constant within the hour, which
redistributes beam between morning- and afternoon-facing planes. Test
thresholds: 1 %, 1.5 %, 2.5 %, 1 % (10° south-east).

### 5.4 Instantaneous values

The library uses instants for `power_production_now` and the `watts`
attribute. Anchors at each hour start: the **published instant** triple
when the sun is ≥ 5° high (otherwise `kt` is numerically unstable), else
the hour average. `kt` and `kd` are interpolated linearly between anchors
and multiplied by E0 · cos Z at the label time. At hour starts this
reproduces the published instants within 0.2 %.

### 5.5 Transposition (plane of array)

Hay–Davies (1980) with isotropic ground reflection, albedo 0.2:

    GTI = DNI·cosθ + DHI·[A·cosθ/cosZ + (1−A)(1+cosβ)/2] + GHI·ρ·(1−cosβ)/2,   A = min(DNI/E0, 1)

with DNI = beam_h / cos Z. Identical to pvlib's `haydavies` to 10⁻⁶
relative for physically consistent inputs. Two deliberate deviations, both
tested:

* A is clamped to 1 (pvlib does not clamp); A > 1 would weight circumsolar
  diffuse above the whole diffuse component.
* Below 1° sun elevation the beam is treated as diffuse, which conserves GHI
  exactly (defect D-02 in the audit).

*Why Hay–Davies:* isotropic transposition underestimates tilted irradiance
on clear days; Perez needs empirical coefficient tables for a marginal gain
at this data resolution. Hay–Davies is the standard middle ground and has an
independent reference implementation to test against.

**Trackers** (the library passes `nan` for a tracked axis): azimuth tracker
faces the sun's azimuth at fixed tilt; tilt tracker sets
β = atan(tan Z · cos(γs − γp)) in [0°, 90°]; dual-axis points at the sun.
Tested: every tracker receives at least the irradiance of the fixed plane
it generalises.

**Azimuth convention:** the library sends Open-Meteo convention
(0 = south, −90 = east). Converted to compass with +180°. Mutation M07
(convention swapped) is caught.

### 5.6 History of elapsed hours

Fusion publishes from the current UTC hour onward. Without a history, the
morning would vanish from "energy today" every hour. `merge_history`:

* elapsed hours keep the last value seen (the latest forecast for that hour);
* current and future hours come **only** from the fresh read — a future hour
  the source stopped publishing is never resurrected;
* pruned before the start of yesterday (local time).

Stored in `.storage/open_meteo_solar_forecast.<entry_id>.local_history`
(≈ 170 hours × ~200 B ≈ 35 kB), saved with a 30 s delay, deleted when the
entry is removed. A corrupt store is discarded with a warning; it never
blocks setup.

### 5.7 Day coverage (D-05)

The library reports 0 Wh for a day it has no data for. With a ~5-day local
horizon the d5–d7 sensors would read 0 (or an undercount for a partly
covered day). In local mode a day-based sensor (`energy_production_today`,
`…_tomorrow`, `…_d2`–`…_d7`, `power_highest_peak_time_today/tomorrow`) is
**unknown** unless every daylight hour of that local date has a valid
average. "Today" after a fresh install, before history covers the morning,
is therefore unknown until the next day — honest rather than low.
`energy_production_today_remaining` (future only) is not masked. Coverage is
persisted with the retained forecast.

### 5.8 Snow

The current snow depth is applied to the whole horizon (persistence). The
library derates linearly to zero at the configured maximum snow cover depth.
Unavailable snow depth is treated as **no snow** and flagged in diagnostics
(fail-open towards an optimistic forecast; see L-5).

---

## 6. Failure modes and behaviour

| # | Condition | Detection | Behaviour (fallback off — default) | With fallback on | Visible in |
|---|---|---|---|---|---|
| F1 | Irradiance entity missing or `unavailable` without series | state lookup | refresh fails → retained forecast; at startup without a retained forecast: setup retry | Open-Meteo used | `forecast_source` = `retained` / `open_meteo_fallback`; `last_error` |
| F2 | Fusion stalled (series not advancing) | first hour older than current hour − 1 h | as F1 | as F1 | `last_error` contains "stale" |
| F3 | Current hour has no valid average | freshness check | as F1 | as F1 | `last_error` |
| F4 | Fewer than 6 future hours | freshness check | as F1 | as F1 | `last_error` |
| F5 | Individual invalid entries / triples | parse | dropped; computation continues; uncovered days unknown | — | diagnostics `rejected` |
| F6 | `weather.get_forecasts` fails | exception | current temperature used for all quarters | — | diagnostics `temperature.error`, `temperature_fallback` count |
| F7 | No temperature at all | no forecast and no current value | as F1 | as F1 | `last_error` |
| F8 | Snow sensor unavailable | parse | treated as 0 (no derating) | — | diagnostics `snow_depth.problem` |
| F9 | Unexpected exception in synthesis/library | generic catch in local path | as F1 (contained, never escapes to HA) | as F1 | `last_error` names the exception type |
| F10 | History store corrupt | load | discarded, warning logged, continues | — | log |
| F11 | Retained forecast older than its horizon | — (pre-existing behaviour) | values decay to 0 | — | `forecast_source` = `retained`, `last_successful_update` |

Logging is transition-based: a warning when a failure starts (or fallback
begins), an info line on recovery — not one line per 10-minute cycle.

---

## 7. ICS considerations

**Network egress.** Local mode performs no internet request. The library's
only network method (`_request`) is overridden; the adapter instance gets no
API key, URL or HTTP session. Tests replace the library's `_request` with a
guard that fails the test if reached, and the test plugin disables sockets.
Fallback to Open-Meteo, if enabled, contacts the configured base URL exactly
as Open-Meteo mode does.

**Trust boundary.** Entity attributes are untrusted input (any integration
could publish them). Everything is type-checked, range-checked and
timestamp-validated before use; rejection reporting is bounded; nothing is
evaluated or used as a path. The validation is unit-tested rule by rule,
and each rule has a mutation in the record.

**Determinism.** The engine is pure; given the same snapshot it produces the
same payload. All arrays of one refresh use one frozen snapshot.

**Resources.** ≈ 0.16 s CPU per array per refresh (measured, 168-hour
snapshot), run in the executor, not on the event loop; ≈ 35 kB storage.

**Dependencies.** No new runtime dependency. pvlib is test-only.

**Privacy.** Diagnostics continue to redact API key and coordinates; the new
`source` block contains entity IDs and data-quality counters only. A test
asserts that the latitude does not appear anywhere in the export.

**Upgrade path.** No migration. Existing entries have no `weather_source`
option and default to Open-Meteo. The retained-forecast fingerprint of an
unchanged entry is identical (tested), so the first refresh after upgrade
behaves exactly as before.

---

## 8. Configuration reference

General step (setup and options): **Weather data source** —
`open_meteo` (default) or `local`.

Local step (only when `local`):

| Option key | Required | Default | Meaning |
|---|---|---|---|
| `local_weather_entity` | yes | — | `weather.*` with hourly forecast (temperature) |
| `local_irradiance_entity` | no | auto-discovered | sensor with `hourly_forecast` series |
| `local_snow_depth_entity` | no | auto-discovered | snow depth sensor |
| `local_fallback_open_meteo` | yes | `false` | use Open-Meteo when local data is unusable |

Switching back to Open-Meteo removes the local keys from the options.

---

## 9. Limitations

| ID | Limitation | Consequence | Mitigation / path |
|---|---|---|---|
| L-1 | Source resolution is hourly | 15-minute structure is synthetic (smooth within the hour) | quantified: ≤ 1.6 % daily energy (§5.3) |
| L-2 | Radiation not bias-corrected | systematic model error passes through | inverter-based learning (Fusion backlog W6) |
| L-3 | Horizon ≈ 5 days | d5 partly, d6–d7 not covered | those sensors read unknown (§5.7) |
| L-4 | Fixed albedo 0.2 | snow-covered ground (0.6–0.8) underestimates ground-reflected light for steep arrays | small for typical roof tilts; backlog |
| L-5 | Snow depth is a persistence forecast | melting / new snow not anticipated | Fusion's own guidance: use as a Dec–Feb "covered" flag |
| L-6 | Fixed UTC offset per refresh (as Open-Meteo `timezone=auto`) | across a DST change, day boundaries beyond it are off by 1 h until the next refresh after the change | identical to Open-Meteo mode |
| L-7 | Temperature is location-level air temperature | cell-temperature model as in the library (Ross, "not so well cooled") | unchanged library behaviour |
| L-8 | Today is unknown on the first day without history | no "today" value until history covers the morning | intentional (§5.7) |

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Fusion changes the series format | Medium | High | strict validation → `LocalDataError` → retained + visible; contract in §4; tests on the exact v0.3.3 shape |
| Silent wrong values from a label shift | Low | High | label tests (M01, M09), end-to-end energy validation, Fusion contract states no re-shift |
| Local mode silently reaches the internet | Low | Medium (ICS) | adapter design; network guard in every local test |
| Open-Meteo mode regresses | Low | High | parity tests on library arguments, interval and fingerprint; mutation M25 |
| Library upgrade changes `_request` signature or response fields | Medium (on upgrade) | High | requirement pinned `==0.1.32`; payload-shape test lists exactly the fields the library reads; re-run suite on any upgrade |
| Users read "unknown" today as a fault | Medium | Low | documented (README, §5.7) |

---

## 11. Open questions for the maintainer

1. **Fallback default.** Off (fail-safe, offline) was chosen. Confirm, or
   prefer availability over offline operation.
2. **Day masking.** Unknown instead of 0 for uncovered days is a behaviour
   difference between the two sources. Confirm, or prefer partial values
   with a flag attribute.
3. **Bias correction against inverter data** (Fusion backlog W6): build it
   here (the solar layer owns geometry and production) or as a separate
   component consuming both?
4. **SRF as a second opinion** beyond diagnostics — e.g. a disagreement
   threshold that marks the forecast as low-confidence?
