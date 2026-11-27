# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.3 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_37_2_ICS_quality_bug_testing_report.md`; release audit: `omsf_v0.1.37.3_release_audit.md`.

**Suite:** 809 tests passing (+156 subtests), with the privacy guard (`confidential.md` present); pyflakes clean; mypy 38 (the known set).

## Replay with real data (not in the repository)

The new `representativeness()` was run on the owner's export of 22 Nov 2026 (station hours, production, model,
day decisions) at five evaluation times: never paused, r 0.90–0.97, window 14/21/28 days as designed, rejected
share ≤ 0.14 (release audit §3). The same data under the 0.1.37.2 rule: r 0.85 (22 Nov), 0.79 (23 Nov), paused.

## New test module `tests/test_v0_1_37_3_station_check.py` (19 tests)

| Test | Proves |
|---|---|
| `test_rejected_days_do_not_pause_the_learning` | two incoherent days marked `not_representative` leave r > 0.95; the same days marked `ok` (= the old rule) pause |
| `test_undecided_days_are_left_out` | days without a decision do not enter the correlation |
| `test_a_station_unlike_the_house_still_pauses_through_the_correlation` | accepted but unrelated production → paused, reason `correlation` |
| `test_share_of_rejected_days` (7 cases) | 3/7 and 4/9 and 7/7 pause; 2/7 and exactly 3/9 (= 1/3) do not; 3/6 (below 7 decided) does not; none decided does not |
| `test_only_not_representative_counts_as_rejected` | snow, fault, incomplete days are decided but not rejected |
| `test_rejected_share_counts_only_the_window` | rejected days before the window do not count |
| `test_window_grows_in_winter_and_stops_at_28_days` | November: 14-day blocks < 40 → window grows in steps of 7 and stops once ≥ 40, never beyond 28 |
| `test_summer_window_stays_at_14_days` | enough blocks → 14 days |
| `test_result_fields` | every result field, empty store |
| `test_derive_state_reasons` | reason text per rule; default unchanged |
| `test_startup_re_evaluates_a_result_of_the_old_rule` | stored old-rule pause is re-evaluated at startup, state recomputed, event `station_representative` |
| `test_startup_keeps_a_result_of_the_new_rule` | a new-rule result and an empty one are left to the nightly run |
| `test_nightly_records_the_reason_in_the_event` | event text with the counts; state reason; the correlation sensor shows window, decided, rejected, reason and both limits |

### Changed

| Test | Change |
|---|---|
| `test_learning_engine.py::test_representativeness` | days marked accepted; time zone passed (new signature) |

## Mutations M381–M395 (new): 15/15 caught

| ID | Mutation | Caught by |
|---|---|---|
| M381 | rejected days counted again (old rule) | `test_rejected_days_do_not_pause_the_learning` |
| M382 | window never grows | `test_window_grows_in_winter_and_stops_at_28_days` |
| M383 | window up to 35 days | `test_result_fields` |
| M384 | growth only below 20 blocks | `test_window_grows_in_winter_and_stops_at_28_days` |
| M385 | exactly 1/3 pauses | `test_share_of_rejected_days[3-9-False]` |
| M386 | share judged on < 7 days | `test_share_of_rejected_days[3-6-False]` |
| M387 | share never pauses | `test_share_of_rejected_days[3-7-True]` |
| M388 | every rejection reason counts | `test_only_not_representative_counts_as_rejected` |
| M389 | share over 28 days whatever the window | `test_rejected_share_counts_only_the_window` |
| M390 | pause reason ignored | `test_share_of_rejected_days[3-7-True]` |
| M391 | hub does not pass the reason | `test_nightly_records_the_reason_in_the_event` |
| M392 | old result not re-evaluated at startup | `test_startup_re_evaluates_a_result_of_the_old_rule` |
| M393 | re-evaluated at every startup | `test_startup_keeps_a_result_of_the_new_rule` |
| M394 | recovery not recorded | `test_startup_re_evaluates_a_result_of_the_old_rule` |
| M395 | rejected-share limit not shown | `test_nightly_records_the_reason_in_the_event` |

## Regression

All 166 earlier mutants in `learning.py`, `learning_hub.py` and `learning_entities.py`: 164 caught, 2 equivalent as documented (M117, M124). M161 (representativeness ignored in the state) and M266 (single-hour blocks) targeted code that moved; both retargeted to the new code and caught.

Record: `tests/mutation/mutation_results_0.1.37.3.json`.
