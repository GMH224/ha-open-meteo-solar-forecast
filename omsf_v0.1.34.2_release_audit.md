# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34.2 — Release Audit (ICS)

**Release type:** learning-model change (inputs, calibration cells, memory).
**Basis:** owner requests and design discussion of 14 Oct 2026:
- "We should have cloud overall as well, as some models don't give cloud
  high / medium / low";
- temperature as an efficiency input; physics check: the cells are heated
  by the sunlight on them, not by air temperature alone and not by UV;
- "make sure we keep data long enough, so that learning can capture
  seasonality and doesn't just throw out learned data after 2 weeks";
- the approximate 3D shading geometry is accepted; hours it shades heavily
  are kept out of the calibration;
- the previous hour is not used for cell temperature (lag 5–10 min is
  inside the hour).

**Design record:** `OMSF_v0_1_34_Learning_Spec.md` §4, §6, §8, §17 rows 16–20.
**Invariant:** nothing outside the learning hub changed. Open-Meteo and
local entries without a hub group are untouched; the learner still acts
only in state `applied`.

## 1. Changes

| Area | Change | Files |
|---|---|---|
| Predictors | + total cloud (`cloud_coverage`, 0–100, scale 30 %); hours without it get no correction | `learning.py`, `tools/learning_replay.py` |
| Stage 1 weights | half-life 90 d (was 56), floor 0.1 (new), season ±45 d floor 0.7 (was ±30 d, 0.5) | `learning.py`, replay tool |
| Stage 2 | 9 cells: sky type (measured kt) × estimated cell temperature Tc = T_air + 0.0342·GHI (< 25, 25–45, ≥ 45 °C); all stored hours, weighted like stage 1; shrinkage by hour count; applied with forecast air temperature + corrected light | `learning.py`, `learning_hub.py` |
| Shading | nightly re-run also without shading (`async_model_hours(shading=False)`, only when the entry has a valid table); hours with > 10 % shading loss left out of stage 2; unshaded model stored, purged, exported | `coordinator.py`, `learning_hub.py`, `learning.py` |
| Memory | retention 760 d (was 400), evidence ≤ 740 d (was 380), store ≤ 16 MB (was 8); store-trim loop cap tied to the retention (was a fixed 400) | `learning.py`, `learning_hub.py` |
| Store | schema 2, `ALGORITHM_VERSION` 2; migration 1 → 2 keeps measurements, production, models, decisions, switches, events; drops archive, 06:00 records, stage 2; gate restarts; event `migrated` | `learning.py`, `learning_hub.py` |
| Entities | calibration state = overcast/cool cell; efficiency suggestion from the clear/warm cell | `learning_entities.py` |
| Version | 0.1.34.2 | `manifest.json` |

## 2. Physics check (owner question, 14 Oct)

| Question | Finding | Decision |
|---|---|---|
| Does temperature belong in the model? | The library already derates power by −0.4 %/K of cell temperature, Tc = Ta + 0.0342·G_POA (Ross, "not so well cooled"). | kept; stage 2 learns what the generic constants miss on this roof |
| Is air temperature the right band variable? | No: clear midday G ≈ 900 W/m² → +31 K; overcast 150 W/m² → +5 K. | bands by estimated cell temperature |
| Is UV the heat source? | No: UV is about 3–5 % of the solar energy and largely absorbed by the glass; heating is visible + near-infrared. UV forecasts follow sun height and cloud, already in the predictors. | UV not used |
| Previous hour (thermal lag)? | Module time constant 5–10 min; a cloud-to-sun jump overestimates the hour's cell temperature by about 3 K (≈ 1 % power), rarely enough to change a 20 K band. | not used; 10-min lag filter on the backlog |
| Horizontal vs plane-of-array light for the band | Tc uses horizontal GHI (one value for all arrays of a group); in winter the 40° South plane gets more than GHI, so the band is conservative (cooler). | accepted, documented as an approximation (spec §6) |

## 3. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Loss of learned data at upgrade | migration keeps every measurement; only data that cannot be completed (forecasts without total cloud) and results derived from them are dropped; a malformed store still goes to quarantine as before |
| Owner authority | the *Apply learning* switch survives the migration (hub test); the gate restarts, so nothing is applied before 28 new days pass the check |
| Shading error leaking into calibration | heavily shaded hours excluded; threshold boundary tested (exactly 10 % stays in) |
| Store growth | 2-year retention ≈ 3–5 MB expected; hard bound 16 MB with trimming and event |
| Untrusted input | total cloud range-checked like the layers; out of range → missing → no correction |
| Determinism / replay | predictors (incl. total) rounded once; replay tool updated (total cloud, floor) and reproduces every stored correction exactly |
| Executor safety | unshaded model in the same awaited coordinator call; stage 2 still on `LearningData.view()` (now incl. `model_free`) |

## 4. Verification

See the test report addendum `omsf_v0_1_34_2_ICS_quality_bug_testing_report.md`.
Summary: 563 tests passing (+152 subtests); pyflakes clean; mypy without new
findings (38 in pre-0.1.34 modules, unchanged). Mutations: 214/217 caught + 3
documented equivalent (M71, M117, M124); new M199–M217 19/19 after three
test additions (M129, M165, M210 survived the first run).

## 5. Defects in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | `build_evidence` reshaped the predictor matrix to a fixed width of 5; with total cloud every correction would have raised (the learner would have stayed raw, visibly failed) | engine tests | width from `PREDICTOR_NAMES` |
| D-2 | First design banded stage 2 by air temperature | owner's physics question | bands by estimated cell temperature |
| D-3 | Store-trim loop capped at 400 days, inconsistent with the new retention | review | cap = `RETENTION_DAYS` |
| D-4 | A first version of the migration logged its event with a clock read in the pure engine (rule 2) | review | event logged by the hub |

## 6. Owner actions

1. Upload 0.1.34.2 (zip), release `v0.1.34.2`, HACS redownload, restart.
2. Learning state shows `collecting` again (gate restarted). Events contain
   `migrated` and `algorithm_changed`. Station hours and production are kept.
3. After the 03:30 run: `Calibration <group>` attributes show 9 cells; the
   production export has a `_model_unshaded_kwh` column.
4. *Apply learning* stays on; it acts once the gate passes.

## 7. Honest gaps

- Not yet run on the live system; the migration is tested with a
  synthetic 0.1.34.1 store, not the owner's file.
- Cell temperature uses horizontal light; wind is not available.
- Winter shading geometry still unverified (January recheck).
