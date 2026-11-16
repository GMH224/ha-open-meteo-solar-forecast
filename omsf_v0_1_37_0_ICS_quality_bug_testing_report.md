# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.0 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_36_0_ICS_quality_bug_testing_report.md`; only what changed.

**Suite:** 700 tests passing (+156 subtests), stable over repeated runs; pyflakes clean; mypy 38 findings, the known
set; privacy guard passing with `confidential.md` present.
**Mutations:** 331 in the catalogue. New M300–M331: 32/32 caught. Regression on the mutants of the changed modules:
155/158 caught + 3 documented equivalent (M71, M117, M124), no regression. Record: `tests/mutation/mutation_results_0.1.37.0.json`.

## New test modules

### `tests/test_solcast.py` (engine, pure)

| Test | Proves |
|---|---|
| `test_module_is_pure` | no Home Assistant, aiohttp or randomness; no clock, no files |
| `test_slots_are_local_times_and_sunrise_plus_40` | slots in local time (summer and winter time), sunrise + 40 |
| `test_a_slot_is_called_once_within_its_window` | not before, within 45 min, once |
| `test_budget_stops_planned_calls_one_below_the_limit_and_keeps_the_reserve` | planned stop at limit − 1; retry uses the reserve; never above the limit; retry only when due |
| `test_budget_counts_per_utc_day` | 01:30 local is still the previous UTC day |
| `test_results_failures_and_one_retry` | retry delay 5–15 min from the jitter; only one pending; success resets |
| `test_parse_forecast` | Solcast format incl. 7-digit fractions; 13 malformed answers rejected |
| `test_latest_issue_and_hour_power` | 12-hour age limit; hour = mean of both half hours |
| `test_buckets_and_lead`, `test_blend_weight_by_lead` | bucket edges; weight per lead |
| `test_solcast_light_by_hand`, `test_blend_shift_by_hand` | kt_s = c·P/E0h, clear-sky cap, none at night; combination and diffuse part by hand |
| `test_record_pair_keeps_the_lead_nearest_the_bucket_centre` | one pair per hour and bucket |
| `test_factor_is_learned_and_shrunk` | c by the formula; prior with few hours |
| `test_weights_follow_the_better_forecast_and_stay_in_range`, `test_weight_by_hand_and_clamps` | Solcast perfect → n/(n+60); ours perfect → ≈ 0; equal → about half; by hand; negative → 0; low sun ignored |
| `test_learning_never_looks_ahead` | later days have no influence |
| `test_gate_passes_when_solcast_is_better_out_of_sample`, `test_gate_fails_when_solcast_adds_nothing`, `test_gate_needs_days_energy_and_recent_days`, `test_gate_needs_the_recent_days`, `test_gate_is_out_of_sample` | the check's rules, each on its own |
| `test_decision_records_once`, `test_section_validation` (19 malformed cases), `test_store_round_trip_old_store_and_malformed_section`, `test_view_copies_the_section`, `test_switch_is_logged`, `test_retention`, `test_export_rows`, `test_state`, `test_settings_export_never_contains_the_key` | store, retention, export, state, no key |

### `tests/test_solcast_hub.py` (Home Assistant, Solcast answers mocked)

| Test | Proves |
|---|---|
| `test_not_configured_never_calls` | no key → no call |
| `test_a_slot_calls_once_with_the_key_and_stores_the_answer` | bearer key, query, answer stored, budget counted, once per slot |
| `test_budget_is_respected_across_a_restart` | the stored budget holds after a reload |
| `test_busy_solcast_gets_one_retry` | 429 → one retry 5–15 min later, no second |
| `test_a_rejected_key_is_not_retried_and_warns_after_three` | 401 → no retry; the key is not in the error; event after 3 failures |
| `test_network_errors_and_bad_answers_never_break_the_run` | network error → retry; malformed answer → not stored |
| `test_combination_in_shadow_until_check_and_switch` | shadow lowers only the report; switch alone or check alone → unchanged; both → served; switch off → back |
| `test_weight_zero_or_no_answer_changes_nothing` | weight 0 or no answer → identical forecast |
| `test_a_failing_combination_leaves_the_forecast` | an exception → forecast unchanged, error reported |
| `test_pairs_and_the_decision_are_recorded` | pairs before sunrise + 1 h, the decision (hours and energies) from then |
| `test_nightly_learns_and_checks` | nightly writes the blend and the check |
| `test_entities` | state sensor attributes without key or site; switch |
| `test_diagnostics_and_export_never_contain_the_key` | key and site absent from diagnostics and every export file |
| `test_full_reset_keeps_budget_and_answers` | budget and answers kept; learning and switch cleared |

### Changed

| Test | Change |
|---|---|
| `test_flow_offers_the_hub_once_an_array_exists_and_validates` | the Solcast page: site required with a key, site format, empty key = off |
| `test_hub_options_flow_walks_the_stored_groups` | the Solcast page in the options; the hub reads the new options |

## Mutations M300–M331 (new)

| ID | Mutation | Caught by |
|---|---|---|
| M300 | planned calls use the whole limit | budget test |
| M301 | retry above the daily limit | budget test |
| M302 | missed slot still called | slot window test |
| M303 | slot called twice | slot window test |
| M304 | call not counted | slot window / hub call test |
| M305 | more than one retry pending | retry test |
| M306 | negative power accepted | parse test |
| M307 | estimates not checked for order | parse test |
| M308 | stale answer used | latest-answer test |
| M309 | hour from one half hour | hour-power test |
| M310 | light not limited to clear sky | Solcast light test |
| M311 | diffuse share from ours only | combination by hand |
| M312 | weight not pulled towards zero | weight tests |
| M313 | weight not clamped | weight by hand |
| M314 | factor not pulled towards the prior | factor test |
| M315 | learning window open to the future | no-look-ahead test |
| M316 | check learns from the judged day | out-of-sample test |
| M317 | check ignores the recent days | recent-days test |
| M318 | check ignores the energy | energy test |
| M319 | check without minimum days | days test |
| M320 | served without its check | hub shadow test |
| M321 | served without the switch | hub shadow test |
| M322 | calls without configuration | hub not-configured test |
| M323 | rejected key retried | hub 401 test |
| M324 | served regardless of the hub | hub shadow test |
| M325 | key not redacted in diagnostics | hub diagnostics test |
| M326 | full reset loses the budget | hub reset test |
| M327 | decision recorded before sunrise + 1 h | hub records test |
| M328 | view shares the pairs | view test |
| M329 | stored section not validated | store test |
| M330 | pairs kept longer | retention test |
| M331 | empty key still configures the site | flow test |

All 32 were caught at the first run. Regression scope: every mutant (M01–M299) in `learning.py`,
`learning_hub.py`, `learning_entities.py`, `diagnostics.py`, `coordinator.py` and `config_flow.py`.

## Gaps

- No call against the real Solcast service in tests; answers follow the documented hobbyist format.
