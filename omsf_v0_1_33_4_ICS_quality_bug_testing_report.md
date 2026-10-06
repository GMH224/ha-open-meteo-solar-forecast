# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.4 — ICS Quality Bug & Testing Report

**Release:** v0.1.33.4 (external audit remediation)
**Date:** 6 October 2026
**Suite:** 266 tests passing (+144 subtests), up from 203
**Static analysis:** pyflakes clean
**Mutation testing:** 72/73 caught + 1 documented equivalent mutant (50 carried, 23 new; main run 69/73)
**Standard applied:** *if I break it, does a test fail?* — plus, new in this
release, *does every external input have a bound?*

---

## 1. Scope

1. **Verification of the external audit** before any change: 14 findings
   reproduced by tests asserting the faulty behaviour (verification copy,
   all 14 passing against 0.1.33.3). The rest checked by code reading.
2. **Remediation**: each reproduction turned into a regression test asserting
   the corrected behaviour.
3. **Regression**: all 0.1.33.3 tests, of which 4 were rewritten because they
   encoded the corrected behaviour (release audit §4).

---

## 2. New test file — `test_v0_1_33_4_audit_remediation.py` (62 tests)

| Finding | Tests | What they prove |
|---|---|---|
| OMSF-001 | 3 | several entries → error without `config_entry_id`; with it only that entry changes; single entry needs no id and defaults to home; unloaded entries never targeted |
| OMSF-002 | 9 | string, out-of-range, NaN, inf, bool, missing key, empty, non-mapping all rejected and not persisted; selector extras (radius) accepted |
| V-1 / OMSF-004 | 3 | hybrid with Open-Meteo down reports the same power as local mode; no complete day still serves local data; a truly empty result → setup retry |
| OMSF-007/008 | 5 | out-of-window entries rejected (past and future, exact edge); oversized list rejected whole; temperature list bounded; oversized series is a local-data error in HA |
| OMSF-009 | 14 | the six shipped sample files keep their classification; inf/nan/elevation/shape/empty → controlled message; directory, missing, oversized, non-UTF-8 → controlled message; endpoint tolerance kept |
| OMSF-012 | 8 | seven malformations of the retained store and a non-mapping root → discarded, entry loads |
| OMSF-003 | 3 | both modes: retained at 5 h 59 min, unavailable + `stale` at 6 h 01 min, source sensor still available with the reason, recovery; stored forecast older than 6 h not served at startup |
| OMSF-013 | 1 | failed history read retried on the next refresh |
| OMSF-015/016 | 8 | defensive `supported_features`; non-finite geometry rejected, `nan` tracker sentinel kept |
| OMSF-018 | 2 | premise (aiohttp text contains the key), sanitising, no key in the source sensor or diagnostics after a real `ClientResponseError` |
| OMSF-019 | 1 | recorder exclusion set |
| OMSF-005/006 | 3 | snow held at 23 h, assumed 0 at 25 h; data quality visible for fallback and healthy inputs |
| T-1 guard | 1 | happy path uses forecast temperatures (accepted = received > 100) |
| helper | 1 | finite-number check rejects bool and non-finite |

---

## 3. Defects found in this pass

| ID | Severity | Found by |
|---|---|---|
| V-1 | High | verification of OMSF-004: reproduction compared hybrid vs local power with Open-Meteo down |
| T-1 | test harness | a new data-quality test failing against expectation; the response was keyed `'w','e','a',…` |
| test-side: stale text in local mode; snow reset by helper | — | first run of the new tests |

---

## 4. What mutation testing could not find

All 15 confirmed findings concerned rules that **did not exist** (input
bounds, an age limit, sanitising, service ownership) or a rule with the wrong
constant (24 vs 23). The 0.1.33.3 mutation record (50/50) was accurate and
still said nothing about them: a mutation needs a rule to perturb. This is
recorded as a method limit; the countermeasure is the boundary checklist in
DEVELOPER.md (rules 6–7) and, for this release, mutations for every new rule.

---

## 5. Mutation testing

All 50 earlier mutations re-run against 0.1.33.4, plus 23 new (M51–M73).
Raw results: `tests/mutation/mutation_results_0.1.33.4.json`.

| # | Mutation | Result | First failing test |
|---|---|---|---|
| M01 | average labelled at the quarter START instead of its end | caught | `test_local_physics_validation.py::test_daily_energy_through_the_library_matches_a_one_minute_reference[30-9…` |
| M02 | first label gets an average (covers the hour before the data) | caught | `test_local_source.py::test_the_first_label_carries_no_average_because_it_would_cover_the_previous_hour` |
| M03 | clearness index not clamped | caught | `test_local_source.py::test_an_implausible_hour_average_is_clamped_and_counted` |
| M04 | D-01 regression: hour clearness on a finer grid than the quarters | caught | `test_local_source.py::test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M05 | D-02 regression: low-sun beam not reassigned to diffuse | caught | `test_local_source.py::test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M06 | anisotropy index not clamped to 1 | caught | `test_local_physics_validation.py::test_anisotropy_is_clamped_where_the_input_implies_dni_above_e0` |
| M07 | azimuth convention: Open-Meteo value used as compass bearing | caught | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M08 | ground-reflected term dropped | caught | `test_local_physics_validation.py::test_hay_davies_transposition_matches_pvlib[7-30-0]` |
| M09 | published instants ignored (hour-average anchor only) | caught | `test_local_source.py::test_instant_values_at_hour_starts_reproduce_the_published_instants` |
| M10 | diffuse-exceeds-global check removed | caught | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change2]` |
| M11 | incomplete triple accepted | caught | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change4]` |
| M12 | booleans accepted as numbers | caught | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change8]` |
| M13 | non-hour-aligned period_start accepted | caught | `test_local_source.py::test_entries_with_bad_timestamps_are_rejected_and_reported[change2-not on a full hour]` |
| M14 | stale-series check removed | caught | `test_hybrid_integration.py::test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo` |
| M15 | current-hour requirement removed | caught | `test_local_source.py::test_a_series_without_the_current_hour_average_is_refused` |
| M16 | history resurrects future hours from cache | caught | `test_local_source.py::test_history_never_resurrects_a_future_hour_the_source_dropped` |
| M17 | history not pruned | caught | `test_local_source.py::test_history_is_pruned_before_keep_from` |
| M18 | Fahrenheit not converted | caught | `test_local_source.py::test_temperature_forecast_converts_fahrenheit_and_kelvin` |
| M19 | temperature gaps bridged without limit | caught | `test_local_source.py::test_temperature_gaps_longer_than_three_hours_are_not_bridged` |
| M20 | snow depth not applied | caught | `test_local_integration.py::test_snow_depth_reduces_output_only_when_snow_derating_is_enabled` |
| M21 | polar day/night can yield sunset == sunrise | caught | `test_local_source.py::test_polar_night_and_polar_day_never_produce_a_zero_length_day` |
| M22 | fallback to Open-Meteo on by default | caught | `test_local_integration.py::test_fallback_is_off_when_the_option_is_absent` |
| M23 | fallback taken although disabled | caught | `test_hybrid_integration.py::test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo` |
| M24 | local refresh interval 30 min | caught | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M25 | Open-Meteo refresh interval changed | caught | `test_local_integration.py::test_open_meteo_mode_is_unchanged_and_never_reads_local_entities` |
| M26 | retained-source state not reported | caught | `test_hybrid_integration.py::test_hybrid_retains_and_recovers_like_local_mode` |
| M27 | history never persisted | caught | `test_local_integration.py::test_history_survives_a_restart` |
| M28 | Fusion companion suffix wrong | caught | `test_hybrid_integration.py::test_config_flow_hybrid_asks_for_the_local_entities` |
| M29 | missing temperature accepted (no current temperature either) | caught *(re-run)* | `test_local_integration.py::test_no_temperature_at_all_is_refused` |
| M30 | history merge skipped (only the fresh series is used) | caught | `test_local_integration.py::test_energy_today_keeps_elapsed_hours_after_the_source_moves_on` |
| M31 | D-05 regression: day sensors not masked in local mode | caught | `test_hybrid_integration.py::test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown` |
| M32 | night hours treated as daylight in day coverage | caught | `test_local_integration.py::test_today_becomes_known_once_history_covers_the_morning` |
| M33 | fallback keeps the local coverage mask on Open-Meteo data | caught | `test_local_integration.py::test_fallback_clears_the_local_day_mask` |
| M34 | day coverage not persisted with the retained forecast | caught | `test_local_integration.py::test_day_coverage_survives_a_restart_with_the_retained_forecast` |
| M35 | D-06 regression: weather service call unbounded | caught | `test_local_integration.py::test_a_hung_weather_service_degrades_within_the_bound` |
| M36 | D-07 regression: missing stored coverage masks every day | caught | `test_local_integration.py::test_a_retained_fallback_forecast_is_not_masked_after_restart` |
| M37 | parity: Open-Meteo failure message changed | caught | `test_local_integration.py::test_open_meteo_mode_failure_text_and_logging_are_unchanged` |
| M38 | hybrid: Open-Meteo overrides a complete local day | caught | `test_hybrid.py::test_complete_local_days_come_from_local_and_all_other_days_from_open_meteo` |
| M39 | hybrid: incomplete Open-Meteo days accepted | caught *(re-run)* | `test_hybrid.py::test_an_incomplete_open_meteo_day_is_not_used` |
| M40 | hybrid: days taken in the source's own timezone | caught | `test_hybrid.py::test_open_meteo_in_another_offset_is_regrouped_into_local_days` |
| M41 | hybrid: Open-Meteo fetched every local cycle | caught | `test_hybrid_integration.py::test_open_meteo_is_refreshed_every_30_minutes_not_every_cycle` |
| M42 | hybrid: no cache after a failed refresh | caught | `test_hybrid_integration.py::test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours` |
| M43 | hybrid: cache used without age limit | caught | `test_hybrid_integration.py::test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours` |
| M44 | hybrid: Open-Meteo failure fails the whole refresh | caught | `test_hybrid_integration.py::test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown` |
| M45 | hybrid behaves like local (no Open-Meteo days) | caught | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M46 | hybrid not treated as a local-based source | caught | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M47 | day sources not persisted | caught | `test_hybrid_integration.py::test_day_sources_survive_a_restart` |
| M48 | 0.1.33.2 retained coverage not read after upgrade | caught | `test_hybrid_integration.py::test_a_retained_forecast_from_0_1_33_2_is_read_as_local_days` |
| M49 | source attribute added in Open-Meteo mode | caught | `test_hybrid_integration.py::test_open_meteo_mode_day_sensors_have_no_source_attribute` |
| M50 | release process: zip_release restored | caught | `test_hybrid_integration.py::test_hacs_installs_from_the_tag_without_a_release_zip` |
| M51 | V-1: partial local days dropped in hybrid | caught | `test_hybrid.py::test_partial_local_days_keep_their_data_but_get_no_day_source` |
| M52 | OMSF-004: empty forecast accepted | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf004_an_empty_result_is_a_failure_not_a_success` |
| M53 | OMSF-007: time window not applied | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf007_entries_outside_the_time_window_are_rejected` |
| M54 | OMSF-008: no size cap | caught | — |
| M55 | OMSF-011: 23 hours count as complete | caught | `test_hybrid.py::test_a_day_with_23_hours_is_not_complete` |
| M56 | OMSF-018: URL query strings not removed | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf018_sanitize_removes_query_strings_and_secrets` |
| M57 | OMSF-018: error length not bounded | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf018_sanitize_removes_query_strings_and_secrets` |
| M58 | OMSF-013: no retry after a failed history read | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf013_history_load_is_retried_after_a_transient_failure` |
| M59 | OMSF-015: malformed supported_features raises | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf015_supported_features_are_parsed_defensively[abc-weather_no_…` |
| M60 | OMSF-016: non-finite geometry accepted at parse | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf016_non_finite_geometry_is_rejected_at_the_boundary[inf]` |
| M61 | OMSF-005: snow depth not held | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf005_snow_depth_is_held_for_24_hours_then_assumed_zero` |
| M62 | OMSF-006: temperature quality always 'forecast' | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf006_degraded_temperature_is_visible_on_the_source_sensor` |
| M63 | OMSF-001: several entries -> silently the last one | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf001_service_with_several_entries_requires_an_entry_id` |
| M64 | OMSF-002: service without schema | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf002_invalid_coordinates_are_rejected_and_not_persisted[locati…` |
| M65 | OMSF-002: bool accepted as coordinate | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf002_invalid_coordinates_are_rejected_and_not_persisted[locati…` |
| M66 | OMSF-003: no retention limit | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf003_retained_forecast_expires_after_six_hours[open_meteo]` |
| M67 | OMSF-003: source sensor unavailable with the others | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf003_retained_forecast_expires_after_six_hours[open_meteo]` |
| M68 | OMSF-009: non-finite horizon values accepted | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf009_shipped_sample_files_keep_their_classification[horizon_wr…` |
| M69 | OMSF-009: horizon elevation range not checked | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf009_bad_content_gives_a_controlled_message[0\t10\n180\t95\n36…` |
| M70 | OMSF-009: horizon file size not capped | caught *(re-run)* | `test_v0_1_33_4_audit_remediation.py::test_omsf009_io_problems_give_a_controlled_message` |
| M71 | OMSF-012: AttributeError not caught for retained data | **equivalent** | — |
| M72 | OMSF-012: retained values not checked for finiteness | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf012_malformed_retained_store_is_discarded[mutation3]` |
| M73 | OMSF-019: 15-minute series recorded | caught | `test_v0_1_33_4_audit_remediation.py::test_omsf019_all_large_series_are_excluded_from_the_recorder` |

Notes: **M29** and **M70** escaped the main run — in both, a second rule produced
the same outcome (the new empty-forecast guard; the row limit), so the tests
could not tell which rule fired. Both tests now assert the specific reason.
**M39** was not applied (constant renamed by the OMSF-011 fix) and was
re-pointed. **M71 is an equivalent mutant:** the new type checks raise
`ValueError` before an `AttributeError` can occur, so removing that catch is
unobservable; it is kept as a redundant layer and recorded here rather than
hidden.

Run history 0.1.33.4: main run 69/73 → after two strengthened tests and one
re-pointed pattern: **72/73 caught, 1 equivalent mutant (justified)**.

---

## 6. What was not tested

- 0.1.33.4 not yet run live; hybrid still verified with an Open-Meteo stub.
- One HA version in the suite.
- OMSF-014 (array count) intentionally not addressed.
- The DST argument (OMSF-010) is verified for the solar geometry of
  25–31 Oct 2026 at this site, not for every latitude.

---

## 7. Reproducing

```bash
pip install -r requirements-test.txt
python -m pytest -q                       # 266 passed, 144 subtests passed
python -m pyflakes custom_components tests
python tests/mutation/run_mutations.py    # ~70 min
```
