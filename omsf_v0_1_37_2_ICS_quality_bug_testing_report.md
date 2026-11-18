# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.2 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_37_1_ICS_quality_bug_testing_report.md`; release audit: `omsf_v0.1.37.2_release_audit.md`.

**Suite:** 790 tests passing (+156 subtests), with `confidential.md` present; pyflakes clean; mypy 38 (known set).

## New test module `tests/test_v0_1_37_2_nightly_timer.py` (11 tests)

| Test | Proves | On 0.1.37.1 |
|---|---|---|
| `test_every_timer_callback_is_marked` | every `_*_cb` of the hub carries `@callback` (independent of the Python version) | fails (`_nightly_cb`) |
| `test_timers_start_their_job_on_the_event_loop` (nightly, hourly, same-day, house light) | each timer, fired through the Home Assistant clock, starts its job from the event-loop thread | nightly fails; others pass (guards) |
| `test_nightly_timer_runs_the_nightly_job` | the 03:30 timer completes a nightly run | error (task exception) |
| `test_overdue_nightly_is_caught_up_by_the_hourly_run` | > 26 h → caught up, one event; next hour nothing | fails |
| `test_nightly_not_overdue_is_left_to_its_timer` | < 26 h → no catch-up | fails (no function) |
| `test_overdue_event_once_per_episode_while_the_catch_up_keeps_failing` | retried once an hour, one event per episode, new episode → new event | fails |
| `test_store_without_nightly_counts_from_its_creation` | no run yet: age from the store's creation | fails |
| `test_hourly_timer_uses_the_catch_up` | end to end through the hourly timer | error |

## Mutations

| ID | Mutation | Result |
|---|---|---|
| M371 | nightly timer without `@callback` (the defect) | caught by `test_every_timer_callback_is_marked` |
| M372 | hourly timer without `@callback` | caught |
| M373 | same-day timer without `@callback` | caught (the suite hangs; the runner now records a 900 s timeout as caught) |
| M374 | house-light timer without `@callback` | caught |
| M375 | startup timer without `@callback` | caught |
| M376 | hourly timer bypasses the catch-up | caught by `test_hourly_timer_uses_the_catch_up` (escaped at first: the clock jump also fired the regular nightly timer; test isolated) |
| M377 | overdue never detected | caught |
| M378 | overdue after 10 days | caught |
| M379 | event every hour | caught |
| M380 | overdue episode never ends | caught |

**Regression:** all 50 earlier mutants in `learning_hub.py`: 50 caught.

## Gap

- The test environment cannot run Python 3.14; the thread check replaces the version-specific failure.
