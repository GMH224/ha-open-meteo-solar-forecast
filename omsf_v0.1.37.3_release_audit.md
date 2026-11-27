# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.3 — Release Audit (ICS)

**Release type:** change of a guard rule (station check), owner-approved 27 Nov 2026.
**Trigger:** learning state `paused`, "station not representative of the house (correlation below limit)", while
the skill gate showed +9.4 % out-of-sample improvement (owner screenshot, 27 Nov).

## 1. Finding (owner data, export of 22 Nov, replayed with the real function)

| Item | Evidence |
|---|---|
| Rule until 0.1.37.2 | correlation of total production with the members' model on station light, 3-hour sums, sun ≥ 10°, last 14 days, **every day**; pause below 0.85 |
| Why it fell | 20 and 21 Nov were broken-cloud days (station overcast while the house had sun, and the reverse); the day check had already rejected both as `not_representative`. With them r = 0.85 on 22 Nov and 0.79 on 23 Nov; without them 0.89 |
| Winter statistics | blocks per 14 days fell from 42 (early Nov) to 26; leaving out one ordinary day moved r between 0.79 and 0.88 |
| Purpose of the check | catch a station *systematically* unlike the house (wrong station, time offset, sensor fault); single unlike days are the day check's job |
| Cost of the false pause | the forecast falls back to raw; the learning's measured +9.4 % is lost |

## 2. Change

| # | Rule | Why |
|---|---|---|
| 1 | correlation over days the day check accepted (`ok`) | a rejected day is already out of the learning and must not pause it |
| 2 | window 14 days, grown by 7 up to 28 while it has fewer than 40 blocks | winter: 1–2 blocks a day |
| 3 | pause also when more than 1/3 of at least 7 decided days in the window were `not_representative` | rule 1 must not hide a systematically bad station; snow, fault and incomplete days are house- or data-side and do not count as rejected |
| 4 | a stored result without `window_days` (earlier rule) is evaluated again at startup | the update takes effect at once |
| 5 | reason text names the rule; events `station_unrepresentative` (with reason) and `station_representative` | traceability |
| 6 | station correlation sensor: `window_days`, `decided_days`, `rejected_days`, `rejected_share`, `reason`, `max_rejected_share` | visibility |

Files: `learning.py` (`representativeness`, `_representative_blocks`, constants, `derive_state`),
`learning_hub.py` (`_async_representativeness`, `_async_refresh_old_representativeness`, state reason),
`learning_entities.py` (attribute), tests, docs, `manifest.json` 0.1.37.3.

## 3. Replay of the owner's data with the new function

| Evaluated | r | blocks | window | rejected / decided | paused |
|---|---|---|---|---|---|
| 1 Nov | 0.969 | 42 | 14 | 0 / 14 | no |
| 15 Nov | 0.942 | 52 | 21 | 1 / 21 | no |
| 21 Nov | 0.903 | 44 | 21 | 2 / 21 | no |
| 22 Nov | 0.908 | 41 | 21 | 3 / 21 | no |
| 23 Nov | 0.934 | 59 | 28 | 3 / 27 | no |

Old rule over 28 Oct–23 Nov: r 0.96 → 0.79, 1 paused day and falling; new rule: never paused, r 0.90–0.97,
rejected share ≤ 0.14.

## 4. Safety analysis (ICS)

| Concern | Assessment |
|---|---|
| Does the change hide a bad station? | No. A station unlike the house on most days shows either a low correlation on the days still accepted, or a rejected share above 1/3; both pause (tests with unrelated production, and with 3 of 7 / 4 of 9 / 7 of 7 rejected days) |
| Independent guard | unchanged: the skill gate compares the corrected forecast with the house's real production out of sample; the learning is applied only while it passes |
| Fail direction | a pause still means the raw forecast; no path makes the forecast use the learning without the gate |
| Small samples | the rejected share needs 7 decided days; the correlation still needs 20 blocks (otherwise no verdict, as before) |
| Store | no schema change; the stored result gains fields; old results re-evaluated once at startup |
| Owner's installation | after the update and restart: state back to `applied` (r ≈ 0.90–0.93 on accepted days, 2–3 of 21–28 days rejected), unless the current days change the picture |

## 5. Verification

809 tests passing (+156 subtests), with the privacy guard (`confidential.md` present); pyflakes clean; mypy 38 (the known set). New tests: 19 in `tests/test_v0_1_37_3_station_check.py`. Mutations M381–M395: 15/15 caught. All 166 earlier mutants in `learning.py`, `learning_hub.py` and `learning_entities.py`: 164 caught, 2 equivalent as documented (M117, M124). M161 (representativeness ignored in the state) and M266 (single-hour blocks) targeted code that moved; both retargeted to the new code and caught. Record: `tests/mutation/mutation_results_0.1.37.3.json`.

## 6. Owner actions

1. Release `v0.1.37.3`, HACS redownload, restart. The startup evaluates the station check with the new rule;
   the state should show `applied` within a minute (events: `station_representative`).
2. The station correlation sensor's attributes show the window, decided and rejected days.

## 7. Open points

- r drifts slowly downwards even on accepted days (0.97 → 0.90 in four weeks). Part of it is likely the house's
  low-sun shade (trees), which grows towards the winter solstice; it belongs to the low-sun learning (house light
  version 3, January). Watch the sensor; the limit is unchanged at 0.85.
