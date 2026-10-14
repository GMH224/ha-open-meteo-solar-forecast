# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34.2 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_34_ICS_quality_bug_testing_report.md` and the
0.1.34.1 addendum; only what changed.

**Suite:** 563 tests passing (+152 subtests); pyflakes clean; mypy: 38
findings, all in pre-0.1.34 modules, identical set (none new).
**Mutations:** 217 in the catalogue; 214 caught + 3 documented equivalent
(M71, M117, M124). Run on 0.1.34.2: all learning mutations M109–M217 and
every coordinator mutation (M22–M83, coordinator changed); the other
M01–M108 target modules not changed by this release, their earlier results
stand. Record: `tests/mutation/mutation_results_0.1.34.2.json`.

## Existing tests adapted

Predictor tuples (+ total cloud), weather-layer tuples (6 values), stage-2
cell names (9 cells), retention (760 days), evidence age (740 days),
recency (90-day half-life, floor 0.1, season 0.7/±45 days), malformed-store
cases (old 3-cell shape and an out-of-bounds factor are separate cases).

## New tests

| Test | Proves |
|---|---|
| `test_hours_without_total_cloud_get_no_predictors` | missing total → no correction |
| `test_total_cloud_separates_analogs` | 1.53 vs 1.47 scale units on total cloud: excluded / included |
| `test_low_cloud_separates_analogs_at_equal_total_cloud` | fog vs high veil with the same total still differ (found M129 surviving) |
| `test_stage2_cells_by_cell_temperature` | band edges 25 / 45 °C, unknown → middle band |
| `test_cell_temperature_follows_sunlight_not_only_air` | Ross formula by hand; same air temperature, different sky → different band |
| `test_shaded_hour` | > 10 % loss excluded, exactly 10 % kept, unknown counts as unshaded |
| `test_stage2_factor_by_hand` (extended) | shaded hours drop out of the cell count |
| `test_apply_stage2_factor_by_bin_of_the_shifted_kt` (extended) | applied band from forecast air temperature + corrected light, both sides of the 45 °C edge |
| `test_stage2_keeps_a_season_learned_long_ago` | a summer factor ten months old still applies |
| `test_stage2_weights_old_hours_with_the_floor_by_hand` | Σw·prod/Σw·model with w = max(0.5^(age/90), 0.1), computed independently |
| `test_store_of_schema_1_is_migrated_keeping_measurements` | migration keeps measurements, production, models, decisions, events; drops archive, 06:00 records, gate |
| `test_retention_is_two_years` | 700 days kept, 761 purged, incl. the unshaded model |
| `test_store_from_0_1_34_1_is_migrated_and_keeps_switch_and_measurements` (hub) | real load path: no quarantine, *Apply learning* stays on, events `migrated` and `algorithm_changed` |
| `test_model_rerun_stores_the_unshaded_model_too` (hub) | one run without shading tables; a second run with them; old stores without the unshaded model get it filled |
| `test_learning_model_rerun_with_and_without_shading` (shading integration) | real library and real shading table: `shading=False` equals an unshaded entry; morning shaded < 70 %, afternoon identical |

## Mutations M199–M217

| ID | Mutation | Caught by |
|---|---|---|
| M199 | total cloud without weight | total-cloud analog test |
| M200 | stage-1 recency floor ignored | recency test |
| M201 | total read from the low layer | parse test |
| M202 | total range not checked | parse test |
| M203 | stage 2 learns by air temperature | stage-2 by-hand / season tests |
| M204 | stage 2 applied by air temperature | apply test |
| M205 | sunlight heating ignored | cell-temperature test |
| M206 | shaded hours not excluded | stage-2 by-hand test |
| M207 | 10 % boundary | `test_shaded_hour` |
| M208 | unshaded model never computed | hub re-run test |
| M209 | old stores not refilled | hub re-run test |
| M210 | `shading=False` ignored | shading integration test (added after it survived) |
| M211 | migration keeps 3-cell stage 2 | migration tests |
| M212 | migration keeps old archive rows | migration tests |
| M213 | retention 400 days | retention tests |
| M214 | evidence age 380 days | evidence-age test |
| M215 | stage-2 weight without floor | weighted by-hand test |
| M216 | unshaded model not purged | retention test |
| M217 | migration flag lost | migration test |

Survivors during the run and their fixes: M129 (low-cloud weight; the new
total-cloud predictor made the old test pass without it) → low-cloud test at
equal total; M165 (stored factor bounds; the old test case failed on the
missing cells instead) → separate cases; M210 → coordinator-level test.

## Replay

`tools/learning_replay.py` reproduces every stored correction exactly
(three scenarios: plain, frozen interval, stage-1 reset) and recomputes the
gate; tampering is detected.

## Gaps

- Migration tested with synthetic 0.1.34.1 stores, not the owner's file.
- No live run of 0.1.34.2 yet.
