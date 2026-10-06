# Open-Meteo Solar Forecast (GMH224 fork) — v0.1.33.x Architecture (ICS)

**Feature:** local weather source — PV forecast from Home Assistant entities
(SwissWeather Fusion ≥ 0.3.3) instead of the Open-Meteo API; from 0.1.33.3
also a hybrid of both.
**Date:** 6 October 2026 (0.1.33.2), updated 6 October 2026 (0.1.33.3, 0.1.33.4)
**Companion documents:**
[`omsf_v0.1.33.2_release_audit.md`](omsf_v0.1.33.2_release_audit.md) (what changed, defects, safety analysis),
[`omsf_v0_1_33_2_ICS_quality_bug_testing_report.md`](omsf_v0_1_33_2_ICS_quality_bug_testing_report.md) (tests, mutation record, gaps),
[`DEVELOPER.md`](DEVELOPER.md) (module map, how to run tests).

---

## 0b. Update for 0.1.33.4 — external audit remediation

An external ICS/OT audit of 0.1.33.3 raised 20 findings. Each was verified
independently (14 by executable reproduction); 15 were confirmed, 2 were
documented design, 1 disputed, 2 declined, and one defect the audit missed
was found (V-1). Triage: `omsf_v0.1.33.3_external_audit_triage.md`. Changes to
this architecture:

- **Invariant R6 relaxed** (owner decision): safety fixes may change
  Open-Meteo mode. Affected: the location service, the retention limit, the
  horizon-file check, retained-store validation, the recorder exclusion.
  Forecast values in Open-Meteo mode are unchanged.
- **Input bounds (R18):** irradiance and temperature lists ≤ 500 entries,
  timestamps within [now − 48 h, now + 10 d]; an oversized list is rejected
  as a whole. §4.1 is extended accordingly.
- **Bounded retention (R19, F15):** a retained forecast is served for at most
  6 h after the last successful refresh, in every mode. Then the forecast
  sensors are unavailable; `forecast_source` stays available and reads
  `stale`. This replaces the unbounded behaviour of F1–F4/F11.
- **Hybrid (R20):** a day neither source covers completely keeps its partial
  local data for intraday values (as in local mode); an Open-Meteo day needs
  all 24 hours; an empty forecast is a failure. §5.9 updated.
- **Error text (R21):** sanitised (no URL query strings, no key-like
  parameters, ≤ 200 characters) before any attribute or diagnostics.
- **Service (R22):** registered once per integration with a schema;
  `config_entry_id` required when more than one entry is loaded.
- **Snow (R23):** last valid depth held 24 h (§5.8, L-5).
- **Data quality (R24):** `forecast_source.data_quality`.
- **Persistence (R25):** retained store and history store validated; history
  read retried up to 3 times.

| ID | Requirement | Verified by |
|---|---|---|
| R18 | Bounded external input | `test_omsf007_*`, `test_omsf008_*`; M53, M54 |
| R19 | Retained forecast ≤ 6 h, then unavailable + `stale` | `test_omsf003_*` (both modes); M66, M67 |
| R20 | Hybrid: partial local data kept; 24-hour completeness; no empty success | `test_v1_*`, `test_omsf004_*`, `test_a_day_with_23_hours_is_not_complete`; M51, M52, M55 |
| R21 | No secrets in attributes/diagnostics | `test_omsf018_*`; M56, M57 |
| R22 | Service targets an explicit, loaded entry; validated input | `test_omsf001_*`, `test_omsf002_*`; M63–M65 |
| R23 | Snow hold 24 h | `test_omsf005_*`; M61 |
| R24 | Degraded inputs visible | `test_omsf006_*`; M62 |
| R25 | Malformed persistence never blocks setup | `test_omsf012_*`, `test_omsf013_*`; M58, M71, M72 |

---

## 0a. Update for 0.1.33.3 — hybrid mode and release process

**Why.** The first live run (6 Oct 2026, owner's installation, three entries
East/South/West) showed SwissWeather Fusion delivering only ~45 hours of
radiation: ICON-CH2 (120 h) was not yet contributing, so only ICON-CH1 (33 h)
and ICON-D2 (~48 h) remained. In local mode that leaves one fully covered day;
days 2–7 are correctly *unknown* (§5.7), which the owner's dashboard card
draws as 0.0 kWh. Even with CH2 working, days 6–7 are beyond any MeteoSwiss
model.

**What.** A third weather source, **hybrid**: every local day that the local
data covers completely comes from the local computation; every other day
comes from Open-Meteo. The two are joined **by whole days at local midnight**
(owner decision), where PV output is zero — no visible step, no day mixing two
sources. Details §5.9; failure modes F12–F14 in §6; requirements R13–R17.

**Release process.** `hacs.json` no longer sets `zip_release`; HACS installs
straight from the tagged `custom_components/` folder like the owner's other
repositories. The inherited `release.yml` workflow (which built the zip and
never ran on the fork) is removed (R17).

Unchanged: Open-Meteo mode and local mode behave exactly as in 0.1.33.2. The
only visible addition in local mode is a `source` attribute (`local`) on the
day sensors.

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
| R13 | 0.1.33.3: days beyond the local horizon are filled from Open-Meteo (hybrid) | owner | `hybrid.merge_hybrid`, coordinator hybrid branch | `test_short_local_horizon_is_completed_by_open_meteo_day_by_day`, `test_long_local_horizon_uses_open_meteo_only_beyond_it`; M45 |
| R14 | Join by whole local days at midnight; a day never mixes sources | owner | `merge_hybrid` | `test_no_day_ever_mixes_two_sources`, `test_the_seam_is_at_midnight_with_no_day_split`; M38, M40 |
| R15 | Open-Meteo part refreshed every 30 min, not every local cycle; its failure never removes the local days | derived | `_async_hybrid_open_meteo` | `test_open_meteo_is_refreshed_every_30_minutes_not_every_cycle`, `test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown`, `test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours`; M41–M44 |
| R16 | Each day shows its source | derived (ICS) | `source` attribute on day sensors, `day_sources` in diagnostics, `hybrid` state | `test_short_local_horizon_…`, `test_diagnostics_show_the_day_sources_and_open_meteo_state`, `test_open_meteo_mode_day_sensors_have_no_source_attribute`; M49 |
| R17 | Releases install via HACS without a manually attached zip | owner (0.1.33.2 backlog item 7) | `hacs.json`, `release.yml` removed | `test_hacs_installs_from_the_tag_without_a_release_zip`; M50 |

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

### 5.9 Hybrid mode (0.1.33.3)

Module `hybrid.py` (pure, no Home Assistant import — enforced by test).

**Day selection.** For each local calendar day (Home Assistant's UTC offset at
refresh time):

1. **Local** if the day is in `complete_local_dates` (§5.7): every daylight
   hour has a valid local average.
2. Otherwise **Open-Meteo**, if the Open-Meteo estimate has at least 23 hourly
   values in that local day (23 admits a DST day; the library omits hours with
   missing model data, so a model that ends mid-day yields an incomplete day).
3. Otherwise the day is absent → day sensors *unknown* (as in local mode).

Consequences worth stating: *today* comes from Open-Meteo until the local
history covers the whole day (first day after installation); a day the local
data covers only partly is taken entirely from Open-Meteo, not spliced.

**Join.** Watts, hourly and 15-minute energy are taken per day from the
selected source and re-expressed in the local estimate's timezone; daily
totals are recomputed from the selected hours exactly as the library computes
them (Σ hourly average power). This stays correct if Open-Meteo's
`timezone=auto` offset differs from Home Assistant's. The join is at local
midnight, where PV output is zero, so the power curve has no step.

**Open-Meteo part.** Uses the entry's own Open-Meteo settings (base URL,
**model**, API key) through the unchanged library path. Fetched at most every
30 minutes and cached between the 10-minute local refreshes, so the request
load equals Open-Meteo mode. A failed fetch never fails the refresh: a cached
estimate up to 3 hours old is used (`stale_cache`); after that the Open-Meteo
days become unknown (`unavailable`) and the forecast continues with the local
days. Warnings are logged on transitions only.

**Local part failing.** Exactly as local mode: retained forecast, or — only
with *Fall back to Open-Meteo* enabled — a full Open-Meteo forecast
(`open_meteo_fallback`, no day masking).

**Provenance.** `day_sources` (per day `local`/`open_meteo`) is exposed as a
`source` attribute on the day sensors, in diagnostics, and persisted with the
retained forecast (the 0.1.33.2 `local_complete_dates` key is still read on
upgrade). `forecast_source` reads `hybrid` when at least one Open-Meteo day is
in the forecast, `local` when none is (e.g. Open-Meteo unavailable); its
attribute `hybrid_open_meteo` gives `fresh` / `cached` / `stale_cache` /
`unavailable`, the fetch time, the error and the Open-Meteo days.

**Model choice matters again in hybrid mode.** The MeteoSwiss models end at
5 days (ICON-CH2 120 h). An entry set to `meteoswiss_icon_ch2` (or a seamless
MeteoSwiss model, if it does not extend beyond CH2 — *not verified*) cannot
fill days 6–7; those stay unknown. `best_match` blends global models and
reaches the full horizon.

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
| F12 | Hybrid: Open-Meteo request fails, cache ≤ 3 h | exception in Open-Meteo part | local days + cached Open-Meteo days | same | `hybrid_open_meteo.open_meteo` = `stale_cache`, `error` |
| F13 | Hybrid: Open-Meteo request fails, no usable cache | as F12 | local days only; other days unknown; refresh succeeds | same | `forecast_source` = `local`, `hybrid_open_meteo` = `unavailable` |
| F14 | Hybrid: Open-Meteo model shorter than the horizon | incomplete days (< 24 h, 0.1.33.4) | those days unknown | same | day sensors `unknown`; `open_meteo_days` lists what was used |
| F15 | Any mode: no successful refresh for > 6 h (0.1.33.4) | age of `last_successful_update` | forecast sensors unavailable; retained data no longer served | same | `forecast_source` = `stale`, `last_error` |
| F16 | Local input oversized or out of window (0.1.33.4) | parser bounds | oversized list → as F1; single out-of-window entries dropped | same | diagnostics `rejected` |

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
**Hybrid** (0.1.33.3) uses the same local step; in hybrid mode the general
step's Open-Meteo settings (base URL, model, API key) are also live, for the
Open-Meteo days.

---

## 9. Limitations

| ID | Limitation | Consequence | Mitigation / path |
|---|---|---|---|
| L-1 | Source resolution is hourly | 15-minute structure is synthetic (smooth within the hour) | quantified: ≤ 1.6 % daily energy (§5.3) |
| L-2 | Radiation not bias-corrected | systematic model error passes through | inverter-based learning (Fusion backlog W6) |
| L-3 | Horizon ≈ 5 days | d5 partly, d6–d7 not covered | those sensors read unknown (§5.7) |
| L-4 | Fixed albedo 0.2 | snow-covered ground (0.6–0.8) underestimates ground-reflected light for steep arrays | small for typical roof tilts; backlog |
| L-5 | Snow depth is a persistence forecast | melting / new snow not anticipated | Fusion's own guidance: use as a Dec–Feb "covered" flag; from 0.1.33.4 the last valid value is held 24 h if the sensor drops out |
| L-6 | Fixed UTC offset per refresh (as Open-Meteo `timezone=auto`) | across a DST change, day boundaries beyond it are off by 1 h until the next refresh after the change. Timestamps themselves are correct instants; the misassigned hour is 00:00–01:00, with the sun far below the horizon, so daily PV totals are unaffected (verified for 25–31 Oct 2026; audit OMSF-010 disputed) | identical to Open-Meteo mode |
| L-7 | Temperature is location-level air temperature | cell-temperature model as in the library (Ross, "not so well cooled") | unchanged library behaviour |
| L-8 | Today is unknown on the first day without history | no "today" value until history covers the morning | intentional (§5.7); in hybrid mode today comes from Open-Meteo instead |
| L-9 | Hybrid: Open-Meteo days limited by the entry's model | days 6–7 unknown with MeteoSwiss-only models | choose `best_match` for the hybrid entries (§5.9) |
| L-10 | Hybrid: a partly local day is taken entirely from Open-Meteo | the local data for that day's covered hours is not used | owner decision: whole-day join, no daylight seam |

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Fusion changes the series format | Medium | High | strict validation → `LocalDataError` → retained + visible; contract in §4; tests on the exact v0.3.3 shape |
| Silent wrong values from a label shift | Low | High | label tests (M01, M09), end-to-end energy validation, Fusion contract states no re-shift |
| Local mode silently reaches the internet | Low | Medium (ICS) | adapter design; network guard in every local test |
| Open-Meteo mode regresses | Low | High | parity tests on library arguments, interval and fingerprint; mutation M25 |
| Library upgrade changes `_request` signature or response fields | Medium (on upgrade) | High | requirement pinned `==0.1.32`; payload-shape test lists exactly the fields the library reads; re-run suite on any upgrade |
| Users read "unknown" today as a fault | Medium | Low | documented (README, §5.7); dashboard cards may draw unknown as 0 (observed live) — hybrid mode removes most unknown days |
| Hybrid hides a local-source failure behind Open-Meteo days | Low | Medium | local failure is handled before the hybrid join (retained / explicit fallback); per-day `source` attribute shows what each day is |
| Hybrid reintroduces internet access | Certain (by design) | Low–Medium (ICS) | opt-in mode; same request load as Open-Meteo mode; local and Open-Meteo modes unchanged |

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
