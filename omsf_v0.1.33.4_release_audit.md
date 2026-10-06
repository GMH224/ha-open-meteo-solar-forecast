# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.4 — Release Audit (ICS)

**Release type:** remediation of the external ICS/OT audit of 0.1.33.3.
**Inputs:** external audit (20 findings), our verification and triage
(`omsf_v0.1.33.3_external_audit_triage.md`), owner decisions of
6 Oct 2026.
**Invariants:** the 0.1.33.1 Open-Meteo-mode invariant is **relaxed for
safety fixes** (owner consent); every deviation is listed in §3. Forecast
*values* in Open-Meteo mode are unchanged.
**Verification:** 266 tests passing (+144 subtests) on Home Assistant
2026.2.3, pyflakes clean, mutation record §6.

---

## 1. How the external audit was handled

No finding was accepted on the auditor's word. Each was checked against the
code; 14 were reproduced by tests asserting the faulty behaviour before any
change was made. Severity was re-assessed for this installation. Result:

| | Count |
|---|---|
| Confirmed and fixed | 15 (incl. OMSF-005/006 as proportionate changes to documented behaviour) |
| Fixed with a deliberate deviation from the proposal | 1 (OMSF-009 endpoint rule) |
| Disputed (documented) | 1 (OMSF-010) |
| Not fixed (backlog, Low) | 1 (OMSF-014) |
| Declined (documented) | 2 (OMSF-017, OMSF-020) |
| Missed by the audit, found in verification | 1 (V-1, High) |
| Found while remediating (test harness) | 1 (T-1) |

Each reproduction became a regression test asserting the corrected behaviour
(`tests/test_v0_1_33_4_audit_remediation.py`).

---

## 2. Changes

| ID | Change | Files |
|---|---|---|
| V-1 / OMSF-004 | Hybrid keeps partial local days for intraday values; empty result = failure | `hybrid.py`, `coordinator.py` |
| OMSF-007/008 | Input bounds: ≤ 500 entries; [now − 48 h, now + 10 d]; oversized list rejected as a whole | `local_source.py`, `local_provider.py` |
| OMSF-011 | Open-Meteo day complete only with 24 hours | `hybrid.py` |
| OMSF-018 | `errors.sanitize_error` for every attribute/diagnostics error text | `errors.py` (new), `coordinator.py`, `local_provider.py` |
| OMSF-001/002 | Service registered once in `async_setup` with schema; explicit target entry | `__init__.py`, `services.yaml`, translations |
| OMSF-003 | Retained forecast ≤ 6 h; then `UpdateFailed`, forecast sensors unavailable, source sensor `stale` (stays available) | `coordinator.py`, `sensor.py`, `const.py`, translations |
| OMSF-009 | Horizon file: regular-file check, 64 KiB, 3600 rows, finite values, elevation −90…90°, controlled message for every I/O error | `coordinator.py` |
| OMSF-012 | Retained store: root/map type checks, finite numbers, aware timestamps; discard on any malformation | `coordinator.py` |
| OMSF-013 | History store read retried up to 3 times | `local_provider.py` |
| OMSF-015/016 | Guarded `supported_features`; non-finite geometry rejected at parse | `local_provider.py`, `local_source.py` |
| OMSF-019 | `wh_period_15m` excluded from the recorder | `recorder.py` |
| OMSF-005 | Snow depth: last valid value held 24 h | `local_provider.py` |
| OMSF-006 | `forecast_source.data_quality` | `local_provider.py`, `sensor.py` |
| T-1 | Test fake for `weather.get_forecasts` fixed; guard test | `tests/` |

---

## 3. Deviations from the Open-Meteo-mode invariant (owner-approved)

| Behaviour in Open-Meteo mode | Before | Now |
|---|---|---|
| Service `update_array_location` with several entries | silently changed the last-loaded entry | error unless `config_entry_id` is given |
| Service input | unvalidated, persisted | schema-validated |
| Prolonged API failure | last forecast served indefinitely | served ≤ 6 h, then sensors unavailable, `forecast_source` = `stale` |
| Invalid horizon file (inf, directory, oversized, …) | some cases crashed setup, `inf` elevation accepted | controlled error message; `inf` rejected |
| Malformed retained store | could block setup permanently | discarded |
| Recorder | stored `wh_period_15m` | excluded |
| Error text in `forecast_source.last_error` | raw (could contain the API key) | sanitised |

Unchanged: library arguments, refresh interval, forecast values, the
failure message and per-failure warning within the 6 h window
(`test_open_meteo_mode_failure_text_and_logging_are_unchanged` still
passes).

**User-visible consequence on this installation:** three entries are
configured, so any automation calling `update_array_location` must now pass
`config_entry_id`.

---

## 4. Tests changed (with reason)

| Test (0.1.33.3) | New name | Reason |
|---|---|---|
| `test_partial_local_data_outside_complete_days_is_dropped` | `test_partial_local_days_keep_their_data_but_get_no_day_source` | asserted the V-1 behaviour |
| `test_without_open_meteo_only_the_local_days_remain` | `test_without_open_meteo_only_complete_local_days_are_listed_as_sources` | same |
| `test_a_23_hour_dst_day_counts_as_complete` | `test_a_day_with_23_hours_is_not_complete` | asserted the OMSF-011 behaviour |
| `test_nothing_from_either_source_is_an_empty_forecast` | `test_nothing_complete_keeps_partial_local_data_but_lists_no_source` (+ `test_no_local_data_and_no_open_meteo_is_an_empty_forecast`) | asserted the OMSF-004 behaviour |
| `FakeFusion._get_forecasts` (fixture) | — | T-1: iterated a string |

All other 199 tests of 0.1.33.3 pass unchanged — now with real forecast
temperatures, which 0.1.33.2/0.1.33.3 integration tests never exercised (T-1).

---

## 5. Defects in this pass's own work

- **T-1 (test harness, since 0.1.33.2).** See §2. Found because a new
  data-quality test expected "forecast" and got "current_value_fallback".
  Not a product defect: the live diagnostics of 6 Oct show 160/160
  temperatures accepted.
- **Two test-side mistakes in the new tests** (local-mode stale error text;
  helper resetting the snow sensor) — fixed before release.
- **OMSF-009 proposal revised** during implementation (endpoint tolerance
  kept, §1).
- **Two tests that could not tell which rule fired** (mutations M29, M70):
  a new rule (empty-forecast guard; row limit) produced the same outcome as
  the rule under test. Both tests now assert the specific reason.
- **One equivalent mutant (M71)**, recorded in §6 rather than removed.

---

## 6. Mutation record

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

## 7. Verification gaps

- Not yet run live (0.1.33.3 is live on the owner's system in local mode).
- Hybrid mode still verified with an Open-Meteo stub only.
- One Home Assistant version in the suite (2026.2.3); live 2026.9.4.
- OMSF-014 (array count) open; OMSF-010 disputed rather than fixed.

---

## 8. Backlog

1. Live acceptance of 0.1.33.4: switch the three entries to **Hybrid**;
   check `forecast_source`, `data_quality`, `day_sources`.
2. LV-1: Fusion still delivers ~48 h (ICON-CH2 radiation missing,
   `current_hour_sources` = 2 at 13:51 UTC on 6 Oct).
3. LV-3: West + South as one entry with a shared 10 kW inverter.
4. Re-tune damping/efficiency against Huawei data.
5. OMSF-014 array cap; bias correction; snow albedo; SRF disagreement flag.
