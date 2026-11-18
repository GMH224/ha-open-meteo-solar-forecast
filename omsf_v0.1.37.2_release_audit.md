# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.2 — Release Audit (ICS)

**Release type:** bug fix with a self-healing guard.
**Trigger:** owner report 18 Nov 2026 ("learning is not working anymore") with the Home Assistant log and the
hub diagnostics.
**Owner decision (18 Nov 2026):** fix, test, self-healing catch-up, mutations, documentation.

## 1. Finding

| Item | Evidence |
|---|---|
| Symptom | log 18 Nov 03:30: `Task was destroyed but it is pending … async_run_nightly`, `RuntimeError: loop … is not the running loop` from `_nightly_cb` on `SyncWorker_3`; diagnostics: stage-1 gate, stage-2, same-day table last computed 16 Nov 23:56 UTC, `last_nightly` report empty, Solcast decision days 1 |
| Cause | in 0.1.36.0 the house-light timer `_house_ghi_cb` was inserted above `_nightly_cb` and took its `@callback` line. A function without the marker is run by Home Assistant in the executor; there `entry.async_create_background_task` is called off the event loop. Python 3.14 (Home Assistant 2026.11) rejects the eager task (`loop is not the running loop`) |
| Time line | 16 Nov 04:30 UTC 0.1.36.0 installed; 16 Nov 23:56 UTC last nightly run (startup catch-up after the 0.1.37.0 update); 17 Nov 02:30 UTC timer failed; 17 Nov 03:10 UTC restart (0.1.37.1): no catch-up (last run 3 h old, limit 20 h); 18 Nov 02:30 UTC timer failed |
| Why the tests missed it | `test_scheduled_runs_fire` fires the 03:30 timer and passed: the test environment (Python 3.13, Home Assistant 2026.2) tolerates a task created from a worker thread. The mutation catalogue had no mutant that removes a timer marker |

## 2. Impact (owner installation)

| Function | Effect |
|---|---|
| Forecast | still corrected: the weather learning stayed *applied* with the values of 16 Nov |
| Learning (stage 1, stage 2, same-day table and check, Solcast weights and check) | not updated for two nights |
| Day decisions | 16 and 17 Nov undecided; decided at the next run (look-back covers them) |
| Measurements (station, production, forecasts, house light) | collected hourly as usual; **nothing lost** |
| Nightly export (house light for Fusion) | not written on 17 and 18 Nov; the next export contains all windows (760-day retention) |

## 3. Changes

| Area | Change | Files |
|---|---|---|
| Fix | `@callback` on `_nightly_cb` | `learning_hub.py` |
| Self-healing | `nightly_overdue()` (last nightly run, or the store's creation, older than 26 h); the hourly timer runs `async_run_hourly_timer()`, which catches up an overdue nightly run, at most once an hour; event `nightly_overdue` and a warning once per episode | `learning_hub.py` |
| Tests | `tests/test_v0_1_37_2_nightly_timer.py` (11) | |
| Mutation runner | a suite that hangs (900 s) counts as caught instead of aborting the run | `tests/mutation/run_mutations.py` |
| Docs | CHANGELOG, architecture §0j (R68–R69, F44), DEVELOPER rule 19 and test table | |
| Version | 0.1.37.2 | `manifest.json` |

## 4. Safety analysis (ICS)

| Concern | Assessment |
|---|---|
| Can the catch-up harm? | It runs the same nightly job as the timer, under the same lock, only when no run finished for 26 h. A normal day never reaches it (the 03:25 hourly run sees a run of about 24 h). If the nightly job itself keeps failing, it is retried once an hour (bounded; the job takes seconds) and the event is written once |
| Startup | unchanged: the startup still catches up a run older than 20 h; the startup's own hourly run does not use the catch-up (no double run) |
| Forecast | unchanged |
| Store | no schema change |

## 5. Verification

790 tests passing (+156 subtests), with the privacy guard; pyflakes clean; mypy 38 (the known set). The new test module was run against 0.1.37.1: 6 tests fail and 2 error there (marker, nightly timer off the event loop — the production error reproduced as an unretrieved task exception — and the catch-up, which did not exist); the 3 that pass on both are the other timers (guards). Mutations M371–M380: 10/10 caught (M373 as a hanging suite; M376 escaped at first because the test clock jump also fired the regular 03:30 timer — test isolated). Regression: all 50 earlier mutants in `learning_hub.py` caught. Record: `tests/mutation/mutation_results_0.1.37.2.json`.

## 6. Owner actions

1. Release `v0.1.37.2`, HACS redownload, restart. The startup catches up the nightly run (the last one is from
   16 Nov), so the learning is current right after the restart; event list: no `nightly_overdue` expected then.
2. Tomorrow after 03:30: the learning status shows a new evaluation time (gate `evaluated` of 19 Nov, ~02:30 UTC).

## 7. Lessons

- A test that fires a timer does not prove the job runs in production; the thread in which it starts must be
  checked, independent of the Python version (DEVELOPER rule 19).
- Moving or inserting decorated functions is checked by a structural test now (all `_*_cb` are callbacks).
- A periodic job whose absence matters gets a watchdog; this one would have repaired itself within an hour.
