# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34.1 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_34_ICS_quality_bug_testing_report.md`; only what
changed.

**Suite:** 540 tests passing (+152 subtests); pyflakes clean; mypy: no new
findings.
**Mutations:** M191–M198 8/8 caught; learning mutations M109–M190 re-run on
the changed code: 80/82 caught + 2 documented equivalent (M117, M124), as in 0.1.34. Whole record: 195/198 caught + 3 equivalent (M71, M117, M124); `tests/mutation/mutation_results_0.1.34.1.json`.

## New tests (7)

| Test | Proves |
|---|---|
| `test_recent_values_are_parsed_record_by_record` | valid records kept, junk/invalid/too old/too far ahead rejected and counted |
| `test_recent_values_list_is_bounded_and_typed` | None, non-list, > 100 entries; exactly 100 accepted |
| `test_a_record_is_replaced_only_by_one_at_least_as_complete` | merge rule both ways; identical record not counted |
| `test_recent_values_fill_the_records_the_latest_value_skipped` | the live situation reproduced: every second record skipped in the state, two hours complete (6 of 6, exact means) through the list |
| `test_without_recent_values_the_strict_rule_leaves_hours_open` | Fusion 0.3.4 behaviour unchanged, rule not relaxed |
| `test_startup_keeps_its_own_report_and_sets_since` | both cosmetic fixes |
| `test_backfill_reads_recent_values_relative_to_the_state_time` | recorder history: the list of a 9-hour-old state is used (found D-1) |

## Mutations M191–M198

| ID | Mutation | Caught by |
|---|---|---|
| M191 | list ignored | skip test |
| M192 | length bound removed | bound test |
| M193 | time window removed | parse test |
| M194 | less complete record replaces a complete one | merge test |
| M195 | startup report overwritten | startup test |
| M196 | `since` not set | startup test |
| M197 | history list judged against now | backfill test |
| M198 | backfill ignores the list | backfill test |

## Gaps

- Fusion 0.3.5's `recent_values` verified against its specification and
  Fusion's own tests, not yet observed live together with this release.
