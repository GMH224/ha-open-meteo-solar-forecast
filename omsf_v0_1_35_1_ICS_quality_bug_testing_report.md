# Open-Meteo Solar Forecast (GMH224 fork) v0.1.35.1 — ICS Test Report (addendum)

Addendum to `omsf_v0_1_35_0_ICS_quality_bug_testing_report.md`; only what changed.

**Suite:** 624 tests passing (+152 subtests); pyflakes clean; mypy 38 findings, the known set in pre-0.1.34 modules
(none in changed code); privacy guard passing with `confidential.md` present.
**Mutations:** 270 in the catalogue. New M264–M270: 7/7 caught. Regression on all learning and same-day mutations
M109–M263: 153/155 caught + 2 documented equivalent (M117, M124), no regression. Record: `tests/mutation/mutation_results_0.1.35.1.json`.

## Tests

| Test | Proves |
|---|---|
| `test_representativeness` (rewritten) | production equal to the model's energy per 3-hour block but shuffled hour by hour → r = 1, representative, blocks ≥ 20, basis named, while the hourly r of the same data is < 0.85; model unrelated to station GHI; random production → paused; empty data → no r, representative; model missing for 2 of 3 hours → no blocks |
| `test_one_band_is_enough_at_sunrise_plus_one_hour` | at sunrise + 1 h one band after sunrise + 30 min; valid with 0.31 kWh clear sky, refused with 0.29 |
| `test_a_pending_newest_band_moves_the_window_back` | newest band without reading → the four bands before it, k = 0.2 by hand; one of two groups pending is enough; an older missing band still refuses; a complete newest band is used |
| `test_signal_uses_confirmed_bands_while_the_newest_is_pending` (hub) | in Home Assistant, 40 s after a band with no counter report since: valid signal from 08:00–09:00, ê < −0.5 |

## Mutations M264–M270

| ID | Mutation | Caught by |
|---|---|---|
| M264 | representativeness against station GHI instead of the model | `test_representativeness` |
| M265 | hourly instead of 3-hour sums | `test_representativeness` |
| M266 | blocks with a single hour used | `test_representativeness` |
| M267 | missing model hours taken as zero | `test_representativeness` |
| M268 | signal needs two bands again | `test_one_band_is_enough_at_sunrise_plus_one_hour` |
| M269 | pending newest band not skipped | `test_a_pending_newest_band_moves_the_window_back` |
| M270 | newest band skipped only when every group is pending | `test_a_pending_newest_band_moves_the_window_back` |

## Gaps

- The hub test models the counter's reporting delay with the absence of readings; the real inverter integration's
  timing is checked on the owner's system (*Same-day correction* at :00/:30).
