# Open-Meteo Solar Forecast (GMH224 fork) v0.1.35.0 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_34_ICS_quality_bug_testing_report.md` and its 0.1.34.x addenda; only what changed.

**Suite:** 621 tests passing (+152 subtests); pyflakes clean; mypy: 38 findings, all in pre-0.1.34 modules, identical set (none in the new or changed code); privacy guard passing with `confidential.md` present.
**Mutations:** 263 in the catalogue; 260 caught + 3 documented equivalent (M71, M117, M124). Run on 0.1.35.0: all learning and same-day mutations M109–M263 and every coordinator mutation (22); the other M01–M108 target modules not changed by this release, their earlier results stand. New M218–M263: 46/46 caught, six of them (M219, M230, M231, M253, M255, M258) only after the tests were strengthened. Record: `tests/mutation/mutation_results_0.1.35.0.json`.

## New test modules

### `tests/test_same_day.py` (engine, pure)

| Test | Proves |
|---|---|
| `test_module_is_pure` | no Home Assistant import, no clock, no file access |
| `test_clear_sky_index_by_hand`, `test_erbs_by_hand` | Haurwitz clear-sky index and Erbs diffuse fraction against the formulas |
| `test_forecast_k_and_its_limits`, `test_curve_records_follow_the_grid` | forecast index, clamp at 1.05, night/missing → none; curve light reproduces its k |
| `test_invert_by_hand`, `test_curves_add_and_total` | inversion on a linear curve, saturation flag, clipping plateau, monotone guard; summing per band and group |
| `test_band_energy_rules` | step function, freshness, reading before the start, negative step, step bound incl. its edge |
| `test_window_starts_thirty_minutes_after_sunrise` | the East inverter's 30-minute wait is respected; window = last four bands |
| `test_signal_by_hand` | ê by hand: k_meas 0.2, k_fc 0.75 (clear-sky-energy weighted over two groups), ê −0.55 |
| `test_signal_refused_with_too_little_light_or_a_missing_counter` | 0.296 kWh refused, 0.304 accepted; missing counter; too early |
| `test_situation` | "improving" at +0.16 within 3 h; not beyond 3 h; not at +0.14 |
| `test_corrected_and_cautious_index_by_hand`, `test_lead_buckets`, `test_prior_is_three_hour_half_life` | k̂ and cautious k by hand incl. clamps and the unconditional spread; bucket edges; prior |
| `test_shifts_move_light_to_the_target_index` | Δkt = (k̂ − k_fc)·kt_clear, Δkd along Erbs, bound 0.6, no-op leaves no entry |
| `test_plan_touches_only_todays_remaining_hours`, `test_plan_decays_with_lead_time` | running hour included, past hours and tomorrow untouched, cautious ≤ served, no signal → nothing served, too few days → no cautious; weight decreases with lead |
| `test_decision_times`, `test_eval_decisions` | sunrise + 1 h, then :00/:30 to 14:00; judged at the first, 10:00 and 12:00 |
| `test_learn_table_by_hand` | β = (1·0.5 + 5·prior)/6 by hand, σ by hand, σ_u, no look-ahead |
| `test_learn_table_clamps_and_separates_situations` | β clamped at 0; situations kept apart |
| `test_learned_persistence_matches_the_error_model` | 60 synthetic AR(1) days through the integration's code: β per bucket = ρ̄·σ²/(σ²+σn²) shrunk as specified (± 0.12) |
| `test_usable_day_and_measured_remaining` | 80 % rule both ways; a missing lit band makes the remaining energy unknown |
| `test_gate_by_hand` | improvement 50 %, coverage, 6 days fail, 4 of 7 better fail, exactly 5 % passes, 4.9 % fails, today and > 28 days excluded |
| `test_section_validation`, `test_malformed_section_rejects_the_store` | eleven malformed cases; a bad section or switch value rejects the whole store |
| `test_store_of_schema_2_is_migrated`, `test_same_day_switch_is_logged`, `test_view_copies_the_same_day_days`, `test_purge_keeps_120_days`, `test_export_rows` | migration keeps measurements and switches; switch event; executor view isolation; retention; export columns |

### `tests/test_same_day_hub.py` (Home Assistant)

| Test | Proves |
|---|---|
| `test_counter_readings_are_kept_with_their_time_and_unit` | Wh → kWh; unavailable, negative and wrong-unit states ignored |
| `test_grey_morning_gives_a_negative_signal_and_a_decision_record` | a grey morning under a clear forecast: valid signal, k_meas < 0.2, ê < −0.5; bands and k stored; decision at 11:00 with remaining energies; shadow only; sensor state |
| `test_served_only_with_check_passed_and_switch_on` | check passed + switch off → unchanged; switch on → lower; a 20-minute-old signal → unchanged again |
| `test_cautious_bands_after_enough_days_never_above_served` | `watts_cautious` appears with 10 days, lower than `watts` |
| `test_nothing_happens_before_sunrise_or_without_member_reports` | before sunrise nothing stored; waiting for members is not a failure |
| `test_a_failing_run_leaves_no_signal_and_the_forecast_unchanged` | a failing model run clears the signal; members serve without SDC |
| `test_nightly_learns_and_checks`, `test_same_day_in_diagnostics_and_store` | nightly step, history, sensor; diagnostics; store round trip |
| `test_a_missed_decision_time_is_not_recorded_late` | 16 min late → not recorded |
| `test_a_band_waits_for_its_counter_then_is_kept_as_unknown` | a band 17 min old waits, one 47 min old is stored unknown |
| `test_cautious_attribute_is_never_above_the_served_bands` | the sensor caps each value at `watts` |

### Changed and added elsewhere

| Test | Change |
|---|---|
| `test_sunrise_by_hand` (engine) | 07:33 local on 7 Oct at the test location, the step before is below −0.833°; midsummer before 05:40 |
| `test_decision_forecast_captured_once_from_sunrise_plus_one_hour`, `test_entry_day_only_same_day_from_sunrise_plus_one_hour` | replace the 06:00 tests |
| hub tests on the decision forecast | captured only after sunrise + 1 h; energy half recorded from then |
| `test_export_writes_the_fixed_folder` | new files; the old `forecast_0600.csv` is removed |
| `test_reset_requires_confirmation_and_restarts_the_gate` | full reset clears the same-day section and switch |
| `test_replay_reproduces_the_same_day_table`, `test_replay_detects_a_tampered_same_day_band` | independent replay of β/σ; tampering detected |
| replay simulation | third daily issue moved to 09:25 local (decision forecast) |

## Mutations M218–M263 (new)

| ID | Mutation | Caught by |
|---|---|---|
| M218 | corrected index not bounded above | `test_corrected_and_cautious_index_by_hand` |
| M219 | persistence weight not bounded when applied | `test_corrected_and_cautious_index_by_hand` (test strengthened after it survived) |
| M220 | cautious quantile wrong (z halved) | `test_corrected_and_cautious_index_by_hand` |
| M221 | signal window starts at sunrise (East still waiting) | `test_window_starts_thirty_minutes_after_sunrise` |
| M222 | minimum clear-sky energy in the window not enforced | `test_signal_refused_with_too_little_light_or_a_missing_counter` |
| M223 | missing counter reading taken as zero | `test_signal_refused_with_too_little_light_or_a_missing_counter` |
| M224 | forecast index of the window not weighted by clear-sky energy | `test_signal_by_hand` |
| M225 | counter freshness ignored | `test_band_energy_rules` |
| M226 | negative counter step accepted | `test_band_energy_rules` |
| M227 | counter step limit removed | `test_band_energy_rules` |
| M228 | saturation not flagged | `test_invert_by_hand` |
| M229 | past hours corrected | `test_plan_touches_only_todays_remaining_hours` |
| M230 | tomorrow corrected | `test_plan_touches_only_todays_remaining_hours` (test strengthened after it survived) |
| M231 | light shift not bounded | `test_shifts_move_light_to_the_target_index` (test strengthened after it survived) |
| M232 | diffuse fraction does not follow the sky | `test_shifts_move_light_to_the_target_index` |
| M233 | learning uses the day it is applied to (look-ahead) | `test_replay_reproduces_the_same_day_table` |
| M234 | no shrinkage toward the prior | `test_replay_reproduces_the_same_day_table` |
| M235 | learned persistence not bounded | `test_replay_reproduces_the_same_day_table` |
| M236 | situations mixed | `test_replay_reproduces_the_same_day_table` |
| M237 | decisions without a valid signal used | `test_replay_reproduces_the_same_day_table` |
| M238 | check ignores the 5-of-7 rule | `test_gate_by_hand` |
| M239 | check ignores the minimum days | `test_gate_by_hand` |
| M240 | check judges today (look-ahead) | `test_gate_by_hand` |
| M241 | usable-day share lowered | `test_usable_day_and_measured_remaining` |
| M242 | remaining energy counted with a missing band | `test_usable_day_and_measured_remaining` |
| M243 | check judges only the first decision | `test_eval_decisions` |
| M244 | first decision at sunrise instead of sunrise + 1 h | `test_decision_times` |
| M245 | stored persistence weight not range-checked | `test_section_validation` |
| M246 | improving threshold lost | `test_situation` |
| M247 | cautious bands with too few days | `test_plan_touches_only_todays_remaining_hours` |
| M248 | decision forecast at sunrise instead of sunrise + 1 h | `test_decision_forecast_captured_once_from_sunrise_plus_one_hour` |
| M249 | migration 2->3 keeps the 06:00 records | `test_store_of_schema_2_is_migrated` |
| M250 | same-day switch setting not validated | `test_malformed_section_rejects_the_store` |
| M251 | same-day section not validated | `test_malformed_section_rejects_the_store` |
| M252 | view shares the same-day bands with the event loop | `test_view_copies_the_same_day_days` |
| M253 | same-day served without its check | `test_served_only_with_check_passed_and_switch_on` (test strengthened after it survived) |
| M254 | same-day served without the switch | `test_served_only_with_check_passed_and_switch_on` |
| M255 | stale signal used | `test_served_only_with_check_passed_and_switch_on` (test strengthened after it survived) |
| M256 | failed run keeps the old signal | `test_a_failing_run_leaves_no_signal_and_the_forecast_unchanged` |
| M257 | counter unit ignored | `test_counter_readings_are_kept_with_their_time_and_unit` |
| M258 | negative counter reading accepted | `test_counter_readings_are_kept_with_their_time_and_unit` (test strengthened after it survived) |
| M259 | missed decision recorded late | `test_a_missed_decision_time_is_not_recorded_late` |
| M260 | band stored before its counter could report | `test_a_band_waits_for_its_counter_then_is_kept_as_unknown` |
| M261 | waiting for members logged as a failure | `test_nothing_happens_before_sunrise_or_without_member_reports` |
| M262 | same-day estimate served regardless of the hub | `test_grey_morning_gives_a_negative_signal_and_a_decision_record` |
| M263 | cautious bands not capped by the served bands | `test_cautious_attribute_is_never_above_the_served_bands` |

## Survivors and what they showed

| Mutant | Why it survived | Test change |
|---|---|---|
| M219 | stored tables are always within [0, 1], so the second clamp was never exercised | tables outside the range applied directly |
| M230 | tomorrow's hours had leads beyond the last bucket (β = 0) | cautious bands and an evening case with tomorrow's morning within the buckets |
| M231 | the test's shift was exactly at the bound | a brighter hour (−0.8 → −0.6) |
| M253 | the switch was never on while the check had not passed | switch on before the check passes → unchanged |
| M255 | moving the test clock fired the hub's own run, which replaced the stale signal | staleness set directly, clock unchanged |
| M258 | the test's states shared one timestamp, so the negative reading was dropped as a duplicate | readings one minute apart |

## Gaps

- No live run; the inverter counter's real update pattern (frequent 0.01 kWh steps) is represented by state changes
  in the test, not captured from the inverter.
- The statistical check uses synthetic AR(1) errors; real cloud errors are bounded and skewed — the coverage and
  improvement sensors check the result on the owner's data.
