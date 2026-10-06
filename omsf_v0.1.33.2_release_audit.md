# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.2 — Release Audit (ICS)

**Release type:** feature (local weather source) plus test-infrastructure
repairs. Baseline: fork 0.1.33.1 (upstream rany2 0.1.33, library
`open_meteo_solar_forecast==0.1.32`, unchanged).
**Governing invariant:** *Open-Meteo mode must behave exactly as in
0.1.33.1* — same library arguments, refresh interval, retained-forecast
fingerprint, error text and logging. Every change was checked against it.
**Verification:** 175 tests passing (+144 subtests) on Home Assistant
2026.2.3, package and tests pyflakes clean, physics validated against pvlib
0.16.1, mutation record in §7 (see the test report for the run log). No
migration, no new runtime dependency.

---

## 1. What was added

| Item | Detail |
|---|---|
| Weather data source option | `weather_source`: `open_meteo` (default) / `local`, in setup and options |
| Local step | weather entity (required), irradiance series sensor, snow-depth sensor (both auto-discovered for SwissWeather Fusion), fallback to Open-Meteo (default off) |
| Local engine (`local_source.py`, pure) | validation, NOAA solar geometry, energy-conserving 1 h → 15 min downscaling, Hay–Davies transposition incl. trackers, Open-Meteo-shaped payload, day coverage |
| HA glue (`local_provider.py`) | live entity reads, bounded weather-service call, history store of elapsed hours, `LocalOpenMeteoSolarForecast` adapter (overrides only the library's network method) |
| Coordinator | source selection, 10-min local / 30-min Open-Meteo interval, fail-safe retained forecast, opt-in fallback, coverage persisted with the retained forecast |
| Sensors | `sensor.<name>_forecast_source` (enum, diagnostic); day sensors unknown for uncovered days in local mode |
| Diagnostics | `source` block: configured/active source, last error, entities, per-read data quality, per-array synthesis counters, SRF vs ICON comparison |
| Translations | English (strings + en) and German for every new string |
| Test suite | 175 tests on a real HA; pvlib validation; mutation runner |
| Documents | architecture (ICS), this audit, ICS test report, DEVELOPER.md, CHANGELOG.md, README section |

**Deliberately not added** (recorded so they are not mistaken for gaps):

- **No fork of the Python library.** The library's PV model is reused
  through a subclass; forking would double the audit surface and split the
  two sources' physics.
- **No bias correction** of radiation. Needs inverter production as ground
  truth (Fusion backlog W6).
- **No use of SRF irradiance in the computation.** Undocumented averaging
  basis (Fusion v0.3.3 audit §2). Diagnostics only.
- **No change to Open-Meteo mode**, including pre-existing defects found in
  it (§4.2). Fixing them would break the invariant; they are on the backlog.

---

## 2. Design decisions with physical justification

Full derivations in the architecture document §5. Summary:

**Adapter, not fork.** The library calls `_request("/v1/forecast", params)`
once per array with that array's lat/lon/tilt/azimuth. The adapter answers
with a payload that follows Open-Meteo's `minutely_15` conventions exactly
(average labelled at the end of its quarter, instant at its label). The
payload-shape test pins the exact field set the library reads.

**GHI and DHI as anchors.** The horizontal energy balance is what must be
conserved; hour-mean DNI × mean cos Z ≠ mean(DNI · cos Z). Published DNI
becomes a consistency diagnostic.

**Clearness-index downscaling.** kt and kd are constant within the hour,
shaped by E0 · cos Z over the same 20 sample instants the quarters use →
exact conservation of hourly GHI and DHI. Quantified end-to-end error
against a 1-minute reference: 0.2 % (south), 0.5 % (east), 1.6 % (45° west).

**Hay–Davies transposition.** Better than isotropic on clear days; Perez
gives a marginal gain at hourly resolution for much more empirical
machinery. Matches pvlib to 10⁻⁶. Two deliberate deviations (A ≤ 1; beam
treated as diffuse below 1° elevation), both tested.

**Content-based freshness.** Fusion's series starts at its current UTC hour.
The integration requires an average for the current hour and that the
series' first hour is no older than one hour before the current one. This
detects a stalled source independently of `last_updated` (which changes only
when the attribute changes), and tolerates one Fusion cycle across the hour
boundary.

**History store.** Fusion does not publish elapsed hours and keeps the
series out of the recorder. Without a local history, "energy today" would
lose the morning every hour. Merge rule: elapsed hours keep their last value;
current and future hours only from the fresh read.

**Fail-safe default.** In an OT context an unexpected outbound connection is
a change in the system's communication profile. Local mode is offline; the
fallback is an explicit choice, visible in the source sensor.

---

## 3. Safety analysis — the Open-Meteo-mode invariant

| Risk | Mitigation | Test |
|---|---|---|
| Library constructed differently | arguments built from one dict, Open-Meteo-only args added explicitly | `test_open_meteo_mode_passes_the_same_library_arguments_as_before` |
| Refresh interval changes | constant per mode | `test_open_meteo_mode_is_unchanged_…`; mutation M25 |
| Retained forecast invalidated at upgrade | no option written to existing entries; fingerprint unchanged | `test_an_existing_entry_keeps_its_retained_forecast_fingerprint` |
| Error text / logging changes | Open-Meteo branch keeps the 0.1.33.1 message and per-failure warning with traceback | `test_open_meteo_mode_failure_text_and_logging_are_unchanged`; mutation M37 |
| Day sensors masked | mask is `None` outside local mode | `test_open_meteo_mode_never_masks_day_sensors` |
| Local code reached in Open-Meteo mode | reader/adapter not constructed | `test_open_meteo_mode_is_unchanged_and_never_reads_local_entities` |

Accepted, visible differences in Open-Meteo mode: one new diagnostic
entity (`forecast_source`) and a new `source` block in the diagnostics
export. Neither changes a forecast value.

Saving the options of an existing entry now writes
`weather_source: open_meteo`, which changes the fingerprint once and causes
one extra Open-Meteo fetch after that save. Saving options already
triggered a reload; no forecast value changes.

---

## 4. Defects

### 4.1 Pre-existing, fixed (test infrastructure and packaging)

**P-01 (Medium) — the fork's test suite could not run.**
`tests/test_config_flow.py` imports `to_field_list` from
`homeassistant.helpers.config_validation`; it does not exist in Home
Assistant 2026.2.3 (not found by search in the installed package). The file
failed at collection, so the existing wizard tests had not been executing in
this environment. **Fix:** import shim falling back to
`voluptuous_serialize.convert`. The three tests run and pass. *Whether
`to_field_list` exists in a later HA release was not verified.*

**P-02 (Low) — legacy tests poisoned later tests.** The unittest-based
`IsolatedAsyncioTestCase` leaves the thread without an event loop; every
Home Assistant fixture after it in the same session failed at setup.
**Fix:** `pytest_runtest_setup` hook restores a loop. Test files unchanged
apart from the shim.

**P-03 (Low) — declared minimum Home Assistant version was wrong.**
`hacs.json` said 2022.11; the options flow relies on the framework setting
`OptionsFlow.config_entry` (framework-provided since about 2024.11, per the Home Assistant developer blog — *not verified against that release*) and the local source
uses `weather.get_forecasts` (2023.12). **Fix:** 2024.12.0.
*(Runtime on 2024.12 not verified; tested on 2026.2.3.)*

### 4.2 Pre-existing, recorded, not fixed (invariant)

**P-04 (Low)** — `checkHorizonFile` opens the horizon file without closing
it (one leaked handle per setup with horizon enabled).

**P-05 (Medium, upstream library 0.1.32)** — instantaneous power is stored
15 minutes early: the library keys the instant value labelled `t` at
`t − 15 min` (its comment says "even for instant data"). If Open-Meteo's
`_instant` variables are values *at* their timestamp (as documented by
Open-Meteo; not verified live), `power_production_now` shows the power
expected 15 minutes later. Both sources are affected identically, because
the local payload deliberately reproduces Open-Meteo's conventions.
Upstream issue candidate.

**P-06 (Low)** — a retained forecast is served without an age limit; after
an outage longer than its horizon, values decay to 0. Now at least visible:
`forecast_source = retained` with `last_successful_update`.

**P-07 (Low)** — the `update_array_location` service is registered at every
entry setup and never removed on unload.

**P-08 (Low, upstream library)** — `peak_production_time` raises
`RuntimeError` for a day without data, so the peak-time sensors raise on
every state write in that case (Open-Meteo mode: only with a retained
forecast older than one day). In local mode the day mask returns unknown
before the library is called, so the case cannot occur there.

### 4.3 New-feature defects found and fixed before release

**D-05 (High for local mode) — days outside the local horizon would read
0 Wh.** The library returns 0 for a day without data and an undercount for
a partly covered day. With Fusion's ~5-day horizon, d5 would be low and
d6/d7 zero — plausible-looking wrong values. **Fix:** day coverage
(`complete_local_dates`), sensors unknown for uncovered days, coverage
persisted with the retained forecast. Mutations M31, M32, M34.

**D-06 (Medium) — unbounded weather-service call.** `weather.get_forecasts`
is a blocking service call without a timeout; a hung weather entity would
have stalled the refresh cycle indefinitely. `SERVICE_TIMEOUT_SECONDS` had
been defined and never used — the "implemented but never reached" class.
**Fix:** 20 s bound on the call (degrades to the current temperature), and
the coordinator's 60 s bound now covers the whole local path. Test runs on
the real clock with a 20 s test timeout; mutation M35.

**D-07 (Low) — restart after fallback masked every day for one cycle.**
A retained forecast from the Open-Meteo fallback has no stored coverage;
it was loaded as "no day covered". **Fix:** absent key → no masking;
malformed → mask all. Mutation M36.

**R-1 (Low, invariant) — Open-Meteo mode's log and error text had changed**
(transition-only logging, new message) in the first implementation. Caught
in review against the invariant. **Fix:** Open-Meteo branch restored
verbatim; local mode keeps transition-only logging. Mutation M37.

---

## 5. Defects in this pass's own work (caught before release)

This section is not empty, and should not be expected to be.

- **D-01 — energy not conserved in the sunrise/sunset hour.** The hourly
  clearness index was integrated over a 1-minute grid, the quarters over
  3-minute samples. Where cos Z is strongly non-linear (sunrise hour) the
  two disagree: 15.03 vs 15.30 W/m² in the test case. **Fix:** one shared
  sample grid; conservation now exact to the output rounding.
- **D-02 — beam energy discarded near the horizon.** Flooring cos Z at 1°
  for DNI = beam / cos Z silently reduced beam below 1° elevation. **Fix:**
  beam treated as diffuse there (conserves GHI).
- **Conservation test too loose.** The first tolerance
  (0.05 + 0.2 % W/m²) let the D-01 regression (mutation M04) pass. Now
  0.01 W/m².
- **Four further weak tests** found by mutation (first run 25/30 caught):
  only GHI was tested as a missing triple member (M11); the boolean test
  case was rejected by a *different* rule, DHI > GHI, so it proved nothing
  (M12); pruning was tested on cached data only (M17); the fallback default
  was never tested because every fixture set the option explicitly (M22).
  All strengthened; second run 33/34 — M33 (fallback must clear the local
  day mask) then got its own test.
- **Test seam hid the code under test.** The first integration tests
  patched `OpenMeteoSolarForecast.estimate`, which the local adapter
  inherits — so the local path was patched away too. Replaced by two
  guards: the coordinator's Open-Meteo fetch is a recorder, and the
  library's `_request` raises if reached.
- **Validation test with impossible input.** A pvlib comparison used
  GHI = 480 W/m² at 10° sun elevation, implying DNI > E0; the disagreement
  looked like an engine error. Inputs made physically consistent; the
  engine's A ≤ 1 clamp now has its own test documenting the deliberate
  deviation from pvlib.
- **A test that could hang the suite.** The D-06 test first used a frozen
  clock, which also freezes asyncio timers. Rewritten on the real clock with
  a test timeout.

### Existing tests changed (with reason)

`tests/test_config_flow.py`: import shim only (P-01). No assertion changed.

---

## 6. Verification record

| Item | Result |
|---|---|
| Test suite | 175 passed, 144 subtests passed (≈ 16 s) |
| Environment | Python 3.13, Home Assistant 2026.2.3 via pytest-homeassistant-custom-component 0.13.316, library 0.1.32, pvlib 0.16.1 |
| Static analysis | `pyflakes custom_components tests` — clean |
| Solar position vs NREL SPA | max 0.012° zenith, 0.015° azimuth (full year) |
| Sunrise/sunset vs SPA | ≤ 1 s observed (threshold 60 s) |
| Hay–Davies vs pvlib | equal to 10⁻⁶ relative (35 cases) |
| End-to-end daily energy vs 1-min reference | +0.2 % / +0.5 % / +1.6 % (south / east / 45° west) |
| Runtime cost | 0.16 s per array per refresh, in the executor |
| Mutation testing | see §7 |

---

## 7. Mutation record

Each mutation is a plausible mistake applied to a copy of the tree, followed
by the full suite. Runner: `tests/mutation/run_mutations.py`. The final run's
complete log, with the first failing test per mutation, is in the test
report §5.

| # | Mutation | Status |
|---|---|---|
| M01 | average labelled at quarter start | caught |
| M02 | first label gets an average | caught |
| M03 | clearness index not clamped | caught |
| M04 | D-01 regression (grid mismatch) | caught *(escaped first run → test tightened)* |
| M05 | D-02 regression (low-sun beam lost) | caught |
| M06 | anisotropy not clamped | caught |
| M07 | azimuth convention swapped | caught |
| M08 | ground-reflected term dropped | caught |
| M09 | published instants ignored | caught |
| M10 | DHI > GHI check removed | caught |
| M11 | incomplete triple accepted | caught *(escaped first run)* |
| M12 | booleans accepted as numbers | caught *(escaped first run)* |
| M13 | non-hour-aligned timestamps accepted | caught |
| M14 | stale-series check removed | caught |
| M15 | current-hour requirement removed | caught |
| M16 | history resurrects future hours | caught |
| M17 | history not pruned | caught *(escaped first run)* |
| M18 | Fahrenheit not converted | caught |
| M19 | temperature gaps bridged without limit | caught |
| M20 | snow depth not applied | caught |
| M21 | zero-length day at the poles | caught |
| M22 | fallback on by default | caught *(escaped first run)* |
| M23 | fallback taken although disabled | caught |
| M24 | local interval 30 min | caught |
| M25 | Open-Meteo interval changed | caught |
| M26 | retained state not reported | caught |
| M27 | history never persisted | caught |
| M28 | Fusion companion suffix wrong | caught |
| M29 | missing temperature accepted | caught |
| M30 | history merge skipped | caught |
| M31 | D-05: day sensors not masked | caught |
| M32 | night hours count as daylight | caught |
| M33 | fallback keeps the local mask | caught *(escaped second run → test added)* |
| M34 | coverage not persisted | caught |
| M35 | D-06: weather call unbounded | caught |
| M36 | D-07: absent coverage masks all | caught |
| M37 | Open-Meteo failure text changed | caught |

---

## 8. Verification gaps (not closable without a live system)

- **No live Home Assistant with the real SwissWeather Fusion.** The
  attribute shape is reproduced from Fusion v0.3.3's source
  (`_compute_solar_forecast`, `SolarIrradianceSensor`), not captured from a
  running instance. *First acceptance step on the target system: compare the
  diagnostics export (`source.local.last_read`) with the live attribute.*
- **Home Assistant versions.** Tested on 2026.2.3 only. The declared
  minimum 2024.12.0 is derived from the APIs used, not run.
- **No comparison against measured production.** Accuracy is validated
  against physics references, not against the owner's inverters.
- **P-05** (instant timing in the library) is derived from reading the
  library and Open-Meteo's documentation, not from a live API response.
- **Translations** other than English and German fall back to English for
  the new strings.

---

## 9. Backlog after v0.1.33.2

1. Bias correction of radiation/production against inverter data
   (Huawei SUN2000 East 5 kW; West + South sharing 10 kW — model as one
   target). Owner question: here or a separate component (architecture §11).
2. P-05 upstream report (instant power 15 min early) — confirm against a
   live Open-Meteo response first.
3. P-04, P-06, P-07, P-08 — Open-Meteo-mode defects, for a release whose
   purpose is to change Open-Meteo mode.
4. Snow-dependent albedo (L-4); snow persistence (L-5).
5. Optional SRF-vs-ICON disagreement flag (architecture §11.4).
6. Capture a live Fusion attribute and add it as a fixture.
