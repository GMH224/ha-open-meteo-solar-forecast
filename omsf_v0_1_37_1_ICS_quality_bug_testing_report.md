# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.1 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_37_0_ICS_quality_bug_testing_report.md`; only what changed. Release audit:
`omsf_v0.1.37.1_release_audit.md` (answer to the external review, finding by finding).

**Suite:** 779 tests passing (+156 subtests), identical in two consecutive runs; pyflakes clean; mypy 38 findings, the known set (one new finding in the first draft was fixed); privacy guard passing with `confidential.md` present.
**Mutations:** 370 in the catalogue. New M332–M370: 38/39 caught + 1 equivalent (M364); M333 and M347 escaped at first and each closed a test gap. Regression on every earlier mutant (M01–M331) in the seven changed modules: 212/215 caught + the 3 documented equivalent (M71, M117, M124), no regression. M87 and M209 targeted code that ICS-07 / ICS-01 replaced; both were retargeted to the new code and are caught. Record: `tests/mutation/mutation_results_0.1.37.1.json`.

## Method

1. Every finding of the review was located in the 0.1.37.0 source and its consumers (who reads the value, what
   happens next), which widened ICS-01 and ICS-04 and enlarged the effect of ICS-05 (release audit §1).
2. A regression test per finding was written against the corrected behaviour and then run on a copy of the
   0.1.37.0 tag: **60 of the first 78 tests failed there** (one more test, the version history, was added later), at least one per finding; the 18 that pass on both
   versions guard unchanged behaviour (complete day, valid store, implausible stored snow ignored, cache bound).
3. Each new rule got its mutations (M332–M370); two escaped at first (M333, M347) and closed a test gap each.
4. Regression: every earlier mutant (M01–M331) in the seven changed modules.

## New test module `tests/test_v0_1_37_1_audit_remediation.py` (79 tests)

| Finding | Test | Proves |
|---|---|---|
| ICS-01 | `test_ics01_station_hours_that_arrive_later_are_modelled` | a half-modelled day is completed when the station's hours arrive; hours already modelled are not changed; an hour in one map only fills only the other map; a complete day causes no model run |
| | `test_ics01_a_missing_station_hour_keeps_the_ratio_check` | a day with one missing station hour and production at 30 % of the model is rejected (`not_representative`); 0.1.37.0 accepted it unchecked |
| | `test_ics01_ratio_uses_only_hours_with_station_and_model` | an hour without station light does not enter the ratio |
| | `test_ics01_complete_day_details_are_unchanged` | complete day: same decision and details as before |
| | `test_ics01_an_hour_with_station_light_waits_for_its_model_then_is_left_out` | waits while the model is missing; after the wait the remaining hours are checked |
| | `test_ics01_too_few_modelled_hours_stay_unchecked` | below 80 % → `ratio_unchecked` as before |
| | `test_ics01_exactly_80_percent_is_checked` | boundary: 80 % checked, below unchecked |
| ICS-02 | `test_ics02_effective_limit_matches_the_forecast_rules` (15 cases) | shared only, none, per array (single, list, one value for several arrays), mixed (array limits win), an array without limit (→ none), non-finite, boolean, negative |
| | `test_ics02_per_array_inverter_gives_the_clipping_flag` | end to end in Home Assistant: per-array limit only → windows at 96 % bad `clipping`, 88 % uncertain; 0.1.37.0: no limit, no flag |
| | `test_ics02_multi_array_entry_limits` | the hub counts the arrays of a multi-array entry (lists, scalar, one-value list) |
| | `test_ics02_house_light_version_2_is_recorded_after_version_1` | rule 16: version 2 recorded with its date after 1; history kept; recorded once |
| ICS-03 | `test_ics03_invalid_json_counts_as_failure_and_retries` (3 bodies) | broken JSON, empty body, invalid UTF-8: failure counted, one retry, budget counted, nothing stored, no key in the error; the retry itself schedules none |
| ICS-04 | `test_ics04_gate_problem` | 16 malformed fields detected by name; absent fields and `improvement: null` accepted |
| | `test_ics04_malformed_check_is_dropped_on_load` (24 cases) | stage-1, same-day and Solcast: the result is dropped, valid history kept, invalid history entries dropped, `repaired` names them; the states compute without error |
| | `test_ics04_valid_checks_load_unchanged` | valid results load unchanged, nothing reported |
| | `test_ics04_history_that_is_not_a_list_is_still_malformed` | structural faults still raise |
| | `test_ics04_hub_drops_the_check_reports_it_and_keeps_the_rest` | in Home Assistant: no quarantine, measurements kept, event `store_repaired`, sensor available |
| ICS-05 | `test_ics05_naive_retained_time_is_discarded_and_fetched_fresh` (3 forms) | entry loads, fresh forecast; 0.1.37.0: setup failed on every retry |
| | `test_ics05_retained_time_with_zone_is_still_used` | a valid retained forecast is still used |
| ICS-06 | `test_ics06_snow_hold_survives_restart_for_the_rest_of_24_hours` | saved with time and sensor; held after a restart 20 h later; ends 24 h after the original reading |
| | `test_ics06_implausible_stored_snow_is_ignored` (12 cases) | other sensor, no zone, future, negative, too deep, NaN, text, boolean, bad time, missing time, wrong types |
| | `test_ics06_future_time_within_clock_slack_is_kept`, `test_ics06_without_snow_sensor_nothing_is_stored_or_restored`, `test_ics06_history_hours_survive_a_bad_snow_entry` | clock slack; no sensor → nothing; a bad snow entry does not discard the history |
| ICS-07 | `test_ics07_same_size_and_time_with_new_content_is_read_again` | same size, restored `mtime_ns`, new content → new table; 0.1.37.0 returned the old one |
| | `test_ics07_unchanged_content_is_not_parsed_again`, `test_ics07_cache_stays_bounded` | the cache still saves the parse; bounded at 32 |

### Changed

| Test | Change |
|---|---|
| `test_solcast.py::test_section_validation` | the case "gate `passed` not a boolean" moved to the common check validation (ICS-04): a malformed gate is now dropped, not a section error that quarantines the store |

## Mutations M332–M370 (new)

| ID | Mutation | Result |
|---|---|---|
| M332 | a day counts as modelled when either map has the hour | caught by `test_model_rerun_stores_the_unshaded_model_too` |
| M333 | hours already modelled are overwritten | caught by `test_ics01_station_hours_that_arrive_later_are_modelled` (escaped at first; test gap closed) |
| M334 | 0.1.37.0 skip (any hour of the day modelled) | caught by `test_ics01_station_hours_that_arrive_later_are_modelled` |
| M335 | no wait for the model of an hour with station light | caught by `test_day_without_station_model_waits_then_is_unchecked` |
| M336 | every daylight hour needs a model (0.1.37.0 strictness) | caught by `test_ics01_a_missing_station_hour_keeps_the_ratio_check` |
| M337 | production summed over all daylight hours | caught by `test_ics01_a_missing_station_hour_keeps_the_ratio_check` |
| M338 | checked hours not reported | caught by `test_ics01_a_missing_station_hour_keeps_the_ratio_check` |
| M339 | exactly 80 % no longer checked | caught by `test_ics01_exactly_80_percent_is_checked` |
| M340 | per-array limits: the largest instead of the sum | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M341 | an array without its own inverter is ignored | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M342 | the shared limit wins over the arrays' own | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M343 | one scalar value counted for one array only | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M344 | a one-value list not repeated for every array | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M345 | a boolean counted as watts | caught by `test_ics02_effective_limit_matches_the_forecast_rules` |
| M346 | the hub ignores per-array inverters (0.1.37.0) | caught by `test_ics02_per_array_inverter_gives_the_clipping_flag` |
| M347 | the hub assumes one array | caught by `test_ics02_multi_array_entry_limits` (escaped at first; test gap closed) |
| M348 | a body that is not JSON is not retried | caught by `test_ics03_invalid_json_counts_as_failure_and_retries` |
| M349 | the JSON error escapes the call (0.1.37.0) | caught by `test_ics03_invalid_json_counts_as_failure_and_retries` |
| M350 | the call does not record the body error | caught by `test_ics03_invalid_json_counts_as_failure_and_retries` |
| M351 | passed not checked | caught by `test_ics04_gate_problem` |
| M352 | a boolean accepted as a count | caught by `test_ics04_gate_problem` |
| M353 | a negative count accepted | caught by `test_ics04_gate_problem` |
| M354 | a non-finite improvement accepted | caught by `test_ics04_gate_problem` |
| M355 | reasons not checked item by item | caught by `test_ics04_gate_problem` |
| M356 | the same-day check not validated | caught by `test_ics04_malformed_check_is_dropped_on_load` |
| M357 | the Solcast check not validated | caught by `test_ics04_malformed_check_is_dropped_on_load` |
| M358 | check history not filtered | caught by `test_ics04_malformed_check_is_dropped_on_load` |
| M359 | the repair is not recorded as an event | caught by `test_ics04_hub_drops_the_check_reports_it_and_keeps_the_rest` |
| M360 | a time without zone accepted (0.1.37.0) | caught by `test_ics05_naive_retained_time_is_discarded_and_fetched_fresh` |
| M361 | the snow value is not saved | caught by `test_ics06_snow_hold_survives_restart_for_the_rest_of_24_hours` |
| M362 | the snow value is not restored (0.1.37.0) | caught by `test_ics06_snow_hold_survives_restart_for_the_rest_of_24_hours` |
| M363 | a value of another sensor restored | caught by `test_ics06_implausible_stored_snow_is_ignored` |
| M364 | a time without zone not rejected explicitly | equivalent: a time without zone cannot be compared with the clock; the error is caught and the value ignored (the explicit check is defence in depth) |
| M365 | a time in the future accepted | caught by `test_ics06_implausible_stored_snow_is_ignored` |
| M366 | the hold restarts at the restart | caught by `test_ics06_snow_hold_survives_restart_for_the_rest_of_24_hours` |
| M367 | a negative depth accepted | caught by `test_ics06_implausible_stored_snow_is_ignored` |
| M368 | the digest covers the start of the file only | caught by `test_ics07_same_size_and_time_with_new_content_is_read_again` |
| M369 | the cache is never used | caught by `test_cached_file_is_not_parsed_again` |
| M370 | the cache grows by one | caught by `test_ics07_cache_stays_bounded` |

## Regression

All 215 earlier mutants in `learning.py`, `learning_hub.py`, `house_ghi.py`, `solcast.py`, `coordinator.py`, `local_provider.py` and `shading_provider.py`: 212 caught, 3 equivalent as documented since 0.1.34/0.1.35 (M71, M117, M124). Retargeted: M87 (shading cache ignores changes; now against the content digest) and M209 (unshaded model not filled; now against the set of modelled hours) — both caught.

## Gaps

- See the release audit §7 (per-array clipping on the total only; unchanged handling of days at the station
  minimum; snow trusted for its 24 h).
