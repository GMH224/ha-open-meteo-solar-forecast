# Open-Meteo Solar Forecast (GMH224 fork) v0.1.36.0 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_35_1_ICS_quality_bug_testing_report.md`; only what changed.

**Suite:** 655 tests passing (+152 subtests), stable over repeated full and file runs; pyflakes clean; mypy 38
findings, the known set (none in new or changed code); privacy guard passing with `confidential.md` present.
**Mutations:** 299 in the catalogue. New M271–M299: 29/29 caught. Regression on every mutant in a changed module (M109–M270 in `learning.py`,
`learning_hub.py`, `learning_entities.py`, plus M255 and M259–M261 whose tests were edited): 114/116 caught + 2
documented equivalent (M117, M124), no regression. Mutants in unchanged modules keep their 0.1.35.1 result.
Record: `tests/mutation/mutation_results_0.1.36.0.json`.
**Test environment:** Python 3.13, `pytest-homeassistant-custom-component` 0.13.316 (Home Assistant 2026.2.3),
the newest framework release for Python 3.13 (newer releases need Python 3.14).

## New test modules

### `tests/test_house_ghi.py` (engine, pure)

| Test | Proves |
|---|---|
| `test_module_is_pure` | no Home Assistant import, no clock, no file access |
| `test_clear_sky_ghi_is_the_window_mean_of_haurwitz`, `test_clear_sky_and_elevation_against_pvlib` | clear-sky GHI = mean of Haurwitz at 5 instants (by hand); within 1 % of pvlib; elevation within 0.1° |
| `test_good_window_by_hand` | k = 0.2 from a linear curve, GHI = 0.2 × 500 = 100 W/m², good |
| `test_bad_rules_and_their_order` | counter (missing or no groups), startup at sunrise + 30 min (edge), low sun at 10° (edge), model without light, snow; a missing counter wins |
| `test_clipping_edges` | 95 % bad (GHI withheld, k kept), 94.99 % / 85 % uncertain, 84.99 % not; unknown limit ignored |
| `test_uncertain_rules_and_their_order` | low sun at 18° (edge), temperature, saturated (k = 1.05), resolution at 0.3 kWh (edge), order clipping → low sun → temperature |
| `test_mixed_sky_by_the_diffuse_fraction` | k 0.2 / 0.4 not mixed, 0.6 mixed, 0.95 not; the rule is on the diffuse fraction |
| `test_snow_verdict` | both hours needed, temperature and ratio edges, every "cannot tell" case |
| `test_station_k_uses_the_same_clear_sky` | station k on the same reference; none at night |
| `test_hour_value`, `test_day_counts` | hour mean and worst quality, missing or bad window → none; counts by quality and reason |
| `test_attributes_and_publication` | all required attributes; window times; hour value; last 8 windows; version; counts; stale at 45 min (edge) → bad `counter`; a bad window is never published |
| `test_section_validation`, `test_malformed_section_rejects_the_store` | 14 malformed cases; a bad section rejects the whole store |
| `test_store_without_the_section_gets_an_empty_one_and_round_trips`, `test_view_copies_the_windows` | no schema change needed; JSON round trip; executor view isolated |
| `test_version_is_noted_once`, `test_retention_is_two_years`, `test_export_rows`, `test_settings_export_names_the_version_and_rules` | version recorded once with date and attributed per window; 760 days (both purges); CSV columns and day counts; settings |

### `tests/test_house_ghi_hub.py` (Home Assistant)

| Test | Proves |
|---|---|
| `test_windows_and_the_sensor` | windows assessed after their counters reported; GHI = k × clear-sky GHI; no reading before → bad `counter`; incomplete window not assessed; sensor state, unit, device and state class, required attributes |
| `test_a_bad_window_makes_the_sensor_unavailable` | bad → `unavailable`; uncertain → value with reason |
| `test_no_forecast_and_no_learning_in_the_value` | model inputs: synthetic clear-sky light, station temperature 8.7 °C only (forecast 12 °C never); stage 2, applied stage 1 and the same-day correction leave a window unchanged |
| `test_without_station_temperature_the_value_is_uncertain` | 10 °C assumed, `temperature` |
| `test_clipping_from_the_inverter_setting` | limit from `inverter_power` (1 kW → 0.25 kWh per window); 96 % bad, 88 % uncertain |
| `test_a_window_waits_for_its_counter_then_is_bad` | waits up to 30 min, then bad `counter`; the extra run at :16 |
| `test_snow_from_the_last_two_hours` | snow from station hours and windows; temperature above 3 °C → none; dull station → previous window decides |
| `test_version_is_recorded_and_a_full_reset_keeps_the_windows` | one `house_ghi_version` event; full reset keeps the windows, clears the same-day section |
| `test_export_and_diagnostics` | `house_ghi.csv` (one row per window), `house_ghi_days.csv`, diagnostics block |
| `test_a_new_signal_reaches_the_members_before_the_decision` | fix A: members refreshed in the run; the decision records the corrected remaining energy (below the uncorrected) |

### Changed

| Test | Change |
|---|---|
| `test_served_only_with_check_passed_and_switch_on` | staleness via `hub.signal_time` |
| `test_nothing_happens_before_sunrise_or_without_member_reports` | "no failure" checked for the same-day step |
| `grey_morning` (shared setup) | waits for runs started by timers while the test clock moves |
| `test_a_missed_decision_time_is_not_recorded_late`, `test_a_band_waits_for_its_counter_then_is_kept_as_unknown` | start from an empty day record |

## Mutations M271–M299 (new)

| ID | Mutation | Caught by |
|---|---|---|
| M271 | no bad limit at low sun | `test_bad_rules_and_their_order` |
| M272 | no uncertain band at low sun | `test_uncertain_rules_and_their_order` |
| M273 | clipping limit itself not bad | `test_clipping_edges` |
| M274 | clipping judged on the least loaded inverter | `test_clipping_edges` |
| M275 | the start-up wait ignored | `test_bad_rules_and_their_order` |
| M276 | a missing counter taken as zero | `test_bad_rules_and_their_order` |
| M277 | snow ignored | `test_bad_rules_and_their_order` |
| M278 | GHI not scaled by the clear-sky GHI | `test_good_window_by_hand` |
| M279 | mixed sky judged on k | `test_uncertain_rules_and_their_order` / `test_mixed_sky_by_the_diffuse_fraction` |
| M280 | clear-sky mean over the wrong number of instants | `test_clear_sky_ghi_is_the_window_mean_of_haurwitz` |
| M281 | snow test without the temperature | `test_snow_verdict` |
| M282 | snow from one hour of two | `test_snow_verdict` |
| M283 | hour value with a missing window | `test_hour_value` |
| M284 | stale window still published | `test_attributes_and_publication` |
| M285 | bad window published | `test_attributes_and_publication` (strengthened, see D-3 of the audit) |
| M286 | version recorded at every run | `test_version_is_noted_once` |
| M287 | stored bad window with a value accepted | `test_section_validation` |
| M288 | retention longer than two years | `test_retention_is_two_years` |
| M289 | model run with the forecast temperature | `test_no_forecast_and_no_learning_in_the_value` |
| M290 | missing station temperature not flagged | `test_without_station_temperature_the_value_is_uncertain` |
| M291 | inverter limit not scaled to the window | `test_clipping_from_the_inverter_setting` |
| M292 | no wait for the counters | `test_a_window_waits_for_its_counter_then_is_bad` |
| M293 | full reset deletes the measurements | `test_version_is_recorded_and_a_full_reset_keeps_the_windows` |
| M294 | members not refreshed on a new signal | `test_a_new_signal_reaches_the_members_before_the_decision` |
| M295 | snow not carried when the station cannot tell | `test_snow_from_the_last_two_hours` |
| M296 | sensor available with a bad window | `test_a_bad_window_makes_the_sensor_unavailable` |
| M297 | stored section not validated | `test_malformed_section_rejects_the_store` |
| M298 | view shares the windows with the event loop | `test_view_copies_the_windows` |
| M299 | retention purge skips the windows | `test_retention_is_two_years` |

## Gaps

- No live run yet; the counters' reporting pattern is simulated by state changes.
- The real-data plausibility check (spec/interface §7) used the same-day correction's k (forecast temperature),
  not the new curves with measured temperature; the difference is the cell-temperature effect (a few percent).
