# Open-Meteo Solar Forecast (GMH224 fork) v0.1.35.1 — Release Audit (ICS)

**Release type:** fix — three defects found in the owner's first data after 0.1.35.0.
**Basis:** owner's export of 21 Oct 2026 (14 days: production, model, station, forecasts, same-day bands and
decisions); analysis in this session; owner approval 21 Oct 2026 ("yes. let's do that").
**Design record:** `OMSF_v0_1_35_Same_Day_Spec.md` §8 rows 10–11; `OMSF_v0_1_34_Learning_Spec.md` row 22;
architecture §0f (R49–R51).
**Invariants:** unchanged from 0.1.35.0. Open-Meteo mode and entries outside a hub group untouched; nothing new is
served; the same-day correction still acts only with its own check passed and its switch on.

## 1. Findings in the owner's data

| # | Observation | Cause | Effect |
|---|---|---|---|
| 1 | Learning state `paused: station` since 20 Oct | station check = hourly r(production, station GHI) = 0.835 < 0.85. Same with the model (0.832), so orientation is not the cause; lag 0 best, so timing is right; 3-hour sums 0.913, daily 0.934 | weather learning not applied although the station represents the house well over hours |
| 2 | No same-day signal at sunrise + 1 h | window needs two bands from sunrise + 30 min; at sunrise + 1 h only one exists | the battery decision time had no correction |
| 3 | Signal invalid at several :00/:30 runs | the run is 40 s after a band ends; the counter had not reported yet, so the newest band had no value | decisions recorded without signal; shadow statistics thinner |

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Station check | production vs the members' model on station light (`model` archive), summed over groups, 3-hour UTC blocks with ≥ 2 hours, ≥ 20 blocks, 14 days, limit 0.85; result adds `blocks`, `basis` | `learning.py` |
| Same-day window | minimum one band (`MIN_WINDOW_BANDS = 1`); clear-sky minimum 0.3 kWh unchanged | `same_day.py` |
| Same-day window | newest band without reading for any group and ended < 15 min ago → window computed for `now − 15 min` | `same_day.py` |
| Tests | representativeness rewritten (hourly shuffle within blocks; model unrelated to GHI); two engine tests, one hub test | `tests/` |
| Mutations | M264–M270 | `tests/mutation/run_mutations.py` |
| Docs | CHANGELOG, README, DEVELOPER rule 15, architecture §0f, both specs | |
| Version | 0.1.35.1 | `manifest.json` |

## 3. Soundness

| Claim | How it is ensured |
|---|---|
| The station check still detects a bad station | a station unlike the house gives a low r in 3-hour sums too (test: random production → paused) |
| It no longer reacts to passing clouds | energy moved between hours within a block leaves r = 1 (test; hourly r of the same data < 0.85) |
| Model, not GHI, is the reference | test data with model unrelated to GHI give r = 1 (M264) |
| Missing data is not zero | hours without model or production are left out; blocks need two hours (M266, M267) |
| One band is not too noisy | 0.3 kWh clear-sky minimum unchanged: counter resolution 0.01 kWh moves k by ≤ 0.033 per counter |
| Skipping the newest band does not hide faults | only the newest band, only while it ended < 15 min ago; an older missing band still refuses the signal; the hub stores a band as unknown after 30 min as before |

## 4. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Learning resumes on an unrepresentative station | limit unchanged; the check keeps its purpose (systematic difference); it is evaluated nightly, as before |
| Signal from a stale window | the window is at most 15 min older than before; the signal's own age limit (15 min) and the check before serving are unchanged |
| Store compatibility | no schema change; the stored `representativeness` value is replaced at the next nightly run |

## 5. Verification

See `omsf_v0_1_35_1_ICS_quality_bug_testing_report.md`. Summary: 624 tests passing (+152 subtests); pyflakes clean;
mypy 38 findings, the known set in pre-0.1.34 modules; privacy guard passing with `confidential.md` present. New
mutations M264–M270 7/7 caught at first run; regression run M109–M263 153/155 caught + 2 documented equivalent (M117, M124).

## 6. Owner actions

1. Release `v0.1.35.1` (tag), HACS redownload, restart.
2. *Station correlation* updates after the next nightly run; then the learning state should leave
   `paused`. If it stays paused, send diagnostics.
3. From the next morning, *Same-day correction* should have a value at sunrise + 1 h and at every :00/:30.

## 7. Honest gaps

- The new station check has seen only the owner's 14 days (r 0.913); its limit 0.85 is unchanged, not re-derived.
- One-band signals at sunrise + 1 h will be noisier than four-band ones; the learned persistence β and the
  check judge this on real days. In December the 0.3 kWh minimum may not be reached at sunrise + 1 h; then there
  is no signal, as before.
- The Fusion grey-day bias (forecast light ≈ 2× station on 19/20 Oct) is not addressed here; review on 16 Nov.
