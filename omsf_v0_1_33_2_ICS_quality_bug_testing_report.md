# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.2 — ICS Quality Bug & Testing Report

**Release:** v0.1.33.2 (local weather source)
**Date:** 6 October 2026
**Suite:** 175 tests passing (+144 subtests). Before this release: 3 tests,
which could not be collected on Home Assistant 2026.2.3 (P-01).
**Static analysis:** `pyflakes custom_components tests`: clean
**Mutation testing:** 37/37 caught (first runs: 25/30, then 33/34; every
escape led to a strengthened or new test)
**Standard applied:** *if I break it, does a test fail?*

---

## 1. Scope of testing

Two questions, tested separately:

1. **Is local mode correct?** Input validation, physics (against an
   independent reference), library integration, failure behaviour,
   persistence, configuration UI.
2. **Is Open-Meteo mode unchanged?** This is the release's safety invariant
   (audit §3). It is tested directly, through library arguments, interval,
   fingerprint, error text, logging and the absence of masking, and
   through mutations M25 and M37.

Environment: Python 3.13; Home Assistant 2026.2.3 through
pytest-homeassistant-custom-component 0.13.316 (real core, recorder-free,
**network sockets disabled**); `open_meteo_solar_forecast==0.1.32`; pvlib
0.16.1 as the independent reference.

---

## 2. Test inventory

| File | Tests | What it proves |
|---|---|---|
| `test_local_source.py` | 85 | Engine, rule by rule. Purity (no HA import, no clock read, checked on the AST and the source text); solar geometry sanity; every validation rule with a negative case; freshness (stale, lag tolerance, current hour, short series); history merge (keep elapsed, never resurrect future, prune, fresh wins); transposition identities (horizontal = GHI, isotropic vertical, N/S, E/W convention, trackers ≥ fixed, dual axis faces the sun); payload shape; label convention (shift test); energy conservation to 0.01 W/m²; instants reproduce published values; all-or-none invariant; temperature interpolation, fallback, gap limit; snow; clamp counting; invalid geometry; day coverage |
| `test_local_physics_validation.py` | 45 | Against pvlib: solar position over a full year vs NREL SPA (< 0.02°/0.03°); sunrise/sunset on 4 dates (< 60 s); Hay–Davies on 35 plane/time cases (10⁻⁶); deliberate anisotropy clamp; **end-to-end daily energy through the real library** vs a 1-minute reference on 4 orientations with a clear and a 50 %-cloud day |
| `test_local_integration.py` | 37 | Real HA: local setup without network; Open-Meteo parity (6 tests); stale at startup / later / recovery; opt-in fallback and its default; missing entity; weather-service failure and hang; no temperature; library exception contained; history across hours, restart, removal, corruption; snow derating on/off; diagnostics content and redaction; east/west multi-array; config flow (discovery, 3 validation errors, Open-Meteo path); options flow (switch back, prefill); day masking; mask persistence and D-07 |
| `test_translations_local.py` | 5 | Every new UI string and every validator error key present in strings.json, en.json, de.json; English matches strings.json |
| `test_config_flow.py` | 3 (+144 subtests) | Pre-existing wizard and translation tests, now running (P-01 shim) |

Fixtures: `tests/fusion_fixtures.py` builds the exact attribute shape that
Fusion v0.3.3 publishes (keys, UTC ISO timestamps, 0.1 rounding, `sources` /
`instant_sources`, source count dropping beyond 33 h). The shape is taken
from Fusion's source code (`_compute_solar_forecast`,
`SolarIrradianceSensor.extra_state_attributes`), **not captured live** (§7).

---

## 3. Defects found during implementation

Details and fixes are in the release audit §4–§5. Here is how each one was
found, which is what this report is for:

| ID | Severity | Found by |
|---|---|---|
| P-01 | Medium | first test run: collection error on the baseline suite |
| P-02 | Low | first combined run: 30+ setup errors after the legacy tests |
| P-03 | Low | review of APIs used vs `hacs.json` |
| P-08 | Low | captured log under mutation M25/M29 ("Error adding entity … peak_time_tomorrow") → library source read |
| D-01 | — (own) | `test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` on first run |
| D-02 | — (own) | same test, after D-01 was fixed |
| D-05 | High (local) | review of `Estimate.day_production` while writing limitations |
| D-06 | Medium | review: `SERVICE_TIMEOUT_SECONDS` defined, never used |
| D-07 | Low | review of the retained-forecast load path |
| R-1 | Low | review against the invariant |

The pattern to note: **D-06 is the "implemented but never reached" class**
that the SwissWeather Fusion reports keep recording. A constant existed,
named for its purpose, and nothing used it. Only reading the code for that
specific question found it. No test would have, because the happy path never
hangs.

---

## 4. Test-quality defects (tests that proved less than they claimed)

| Test (first version) | Problem | Found by | Fix |
|---|---|---|---|
| conservation test | tolerance 0.05 + 0.2 % let the D-01 regression pass | M04 escaped | 0.01 W/m² (exact by construction) |
| invalid-triple cases | only GHI missing was tested | M11 escaped | DNI and DHI cases added |
| `{"ghi": True}` | rejected by the DHI > GHI rule, not the bool rule | M12 escaped | `{"dni": True}` |
| pruning test | cached data only | M17 escaped | fresh old entry added |
| fallback tests | every fixture set the option explicitly | M22 escaped | test with the option absent |
| fallback recovery | mask clearing not asserted | M33 escaped | `test_fallback_clears_the_local_day_mask` |
| integration harness | patched `OpenMeteoSolarForecast.estimate`, which the adapter inherits, so the local path was patched away | first integration run (every local test "failed" with the guard message) | patch the coordinator's Open-Meteo fetch + guard the library's `_request` |
| Hay–Davies comparison | GHI 480 W/m² at 10° elevation ⇒ DNI > E0 | 4 failures at 07 UTC | physically consistent inputs + dedicated clamp test |
| hang test | frozen clock also froze asyncio timers → suite hung | full-suite timeout | real clock, `release` event, 20 s test timeout |
| end-to-end validation | `asyncio.run()` closed the plugin's loop | collection errors | native async test |

---

## 5. Mutation testing

Runner: `tests/mutation/run_mutations.py`. Each mutation is applied to a
temporary copy of the tree (the working tree is never touched), then the
full suite runs with `-x`. "First failing test" is therefore the first in
collection order, not necessarily the most specific one. If a pattern does
not match exactly once, the mutation is reported as NOT APPLIED rather than
silently skipped. That happened once: M26 after the R-1 change; its pattern
was updated and it was re-run alone. Raw results:
`tests/mutation/mutation_results_0.1.33.2.json`.

| # | Mutation | First failing test |
|---|---|---|
| M01 | average labelled at the quarter start | `test_daily_energy_through_the_library_matches_a_one_minute_reference[30-90]` |
| M02 | first label gets an average | `test_the_first_label_carries_no_average_because_it_would_cover_the_previous_hour` |
| M03 | clearness index not clamped | `test_an_implausible_hour_average_is_clamped_and_counted` |
| M04 | D-01 regression | `test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M05 | D-02 regression | `test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M06 | anisotropy not clamped | `test_anisotropy_is_clamped_where_the_input_implies_dni_above_e0` |
| M07 | azimuth convention swapped | `test_local_mode_produces_a_forecast_without_contacting_open_meteo` |
| M08 | ground-reflected term dropped | `test_hay_davies_transposition_matches_pvlib[7-30-0]` |
| M09 | published instants ignored | `test_instant_values_at_hour_starts_reproduce_the_published_instants` |
| M10 | DHI > GHI check removed | `test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change2]` |
| M11 | incomplete triple accepted | `…_triple_is_dropped_but_the_instant_survives[change4]` |
| M12 | booleans accepted | `…_triple_is_dropped_but_the_instant_survives[change8]` |
| M13 | non-hour-aligned timestamps | `test_entries_with_bad_timestamps_are_rejected_and_reported[…full hour]` |
| M14 | stale check removed | `test_stale_local_data_at_startup_without_fallback_keeps_the_entry_retrying` |
| M15 | current-hour requirement removed | `test_a_series_without_the_current_hour_average_is_refused` |
| M16 | history resurrects future hours | `test_history_never_resurrects_a_future_hour_the_source_dropped` |
| M17 | history not pruned | `test_history_is_pruned_before_keep_from` |
| M18 | Fahrenheit not converted | `test_temperature_forecast_converts_fahrenheit_and_kelvin` |
| M19 | temperature gaps bridged | `test_temperature_gaps_longer_than_three_hours_are_not_bridged` |
| M20 | snow not applied | `test_snow_depth_reduces_output_only_when_snow_derating_is_enabled` |
| M21 | zero-length polar day | `test_polar_night_and_polar_day_never_produce_a_zero_length_day` |
| M22 | fallback on by default | `test_fallback_is_off_when_the_option_is_absent` |
| M23 | fallback although disabled | `test_stale_local_data_at_startup_without_fallback_keeps_the_entry_retrying` |
| M24 | local interval 30 min | `test_local_mode_produces_a_forecast_without_contacting_open_meteo` |
| M25 | Open-Meteo interval changed | `test_open_meteo_mode_is_unchanged_and_never_reads_local_entities` |
| M26 | retained state not reported | `test_stale_local_data_later_serves_the_retained_forecast_and_says_so` |
| M27 | history never persisted | `test_history_survives_a_restart` |
| M28 | companion suffix wrong | `test_config_flow_local_auto_discovers_the_fusion_sensors` |
| M29 | missing temperature accepted | `test_no_temperature_at_all_is_refused` |
| M30 | history merge skipped | `test_energy_today_keeps_elapsed_hours_after_the_source_moves_on` |
| M31 | D-05: no day masking | `test_days_beyond_the_local_horizon_are_unknown_not_zero` |
| M32 | night counts as daylight | `test_today_becomes_known_once_history_covers_the_morning` |
| M33 | fallback keeps local mask | `test_fallback_clears_the_local_day_mask` |
| M34 | coverage not persisted | `test_day_coverage_survives_a_restart_with_the_retained_forecast` |
| M35 | D-06: unbounded service call | `test_a_hung_weather_service_degrades_within_the_bound` |
| M36 | D-07: absent coverage masks all | `test_a_retained_fallback_forecast_is_not_masked_after_restart` |
| M37 | Open-Meteo failure text changed | `test_open_meteo_mode_failure_text_and_logging_are_unchanged` |

History of the record: run 1 (M01–M30): 25/30. Run 2 (M01–M34): 33/34.
Run 3 (M01–M37): 36/37 applied and caught, plus M26 re-applied: **37/37**.

---

## 6. Anti-patterns avoided

- **No assertion that a crash could satisfy.** Every "None/unknown"
  assertion is paired with a positive one in the same test (e.g. unknown d6
  next to a positive d4 and a positive *remaining today*).
- **No wall-clock thresholds** for performance. The 0.16 s/array figure is
  a recorded measurement, not an assertion.
- **The invariant is tested by behaviour, not by reading source text.** The
  two source-text checks that exist (purity of the engine) are about
  imports and clock calls, where text is the property.
- **Network isolation is asserted, not assumed.** The plugin blocks sockets,
  and every local test additionally guards the library's `_request`.

---

## 7. What was not tested

Stated plainly, because a test report that only lists what was verified is
half a report.

- **No live Home Assistant with the real SwissWeather Fusion.** The input
  shape is reproduced from Fusion's source. First acceptance step on the
  target system: diagnostics export → `source.local.last_read.irradiance`
  should show `received ≈ 120`, `accepted_average ≈ 120`, `rejected: []`,
  and `current_hour_sources = 3` during the day.
- **One Home Assistant version (2026.2.3).** The declared minimum 2024.12.0
  is derived, not run.
- **No accuracy against measured production.** Physics is validated against
  pvlib. Forecast skill depends on the ICON radiation, which is not
  bias-corrected. The owner's inverters are the natural reference (backlog).
- **DST transition inside the horizon** is not tested explicitly. The
  behaviour (fixed offset per refresh) matches Open-Meteo mode's.
- **Long-running behaviour** (days of 10-minute cycles, store growth) is
  covered by the pruning logic and its tests, not by a soak test.
- **UI rendering** of the new form and translations in the frontend is not
  tested. Schema and translation keys are.

---

## 8. Reproducing this report

```bash
pip install -r requirements-test.txt
python -m pytest -q                       # 175 passed, 144 subtests passed
python -m pyflakes custom_components tests
python tests/mutation/run_mutations.py    # ~25-30 min, writes mutation_results.json
```
