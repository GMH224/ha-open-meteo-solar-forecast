# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34 — ICS Quality Bug & Testing Report

**Release:** v0.1.34 (learning hub; Home Assistant platform hardening)
**Date:** 13 October 2026
**Suite:** 533 tests passing (+152 subtests), up from 392
**Static analysis:** pyflakes clean (`custom_components`, `tests`, `tools`);
mypy (`mypy.ini`): new and changed modules without findings under strict
options; 38 findings in older modules, set-identical to 0.1.33.5 (backlog)
**Mutation testing:** 187/190 caught on the final code + 3 documented
equivalent mutants (M71 since 0.1.33.4; M117, M124 new); learning
mutations M109–M190: 80/82 + 2 equivalent
**Standard applied:** *if I break it, does a test fail?*, *does every
external input have a bound?*, and for learning additionally: *can the
learner see the future?* and *can a learner fault ever reach the forecast?*

---

## 1. Scope

1. **Engine** (`learning.py`, pure): parsing of cloud layers and station
   values, hour alignment, predictors, archive, analog ensemble (distance,
   K, shrinkage, recency/season, pooling, bounds, determinism), application
   to hour records (kt/kd space, instants, stage-2 factor, parity), stage 2,
   quality control, representativeness, skill gate, state machine, store
   validation, retention, export text.
2. **No look-ahead and reproducibility:** evidence only after its day's
   decision and before the issue time; freeze and reset; an independent
   re-implementation (`tools/learning_replay.py`) reproduces every stored
   correction and the gate from the export.
3. **Home Assistant** (real HA 2026.2.3): hub setup and entities, shadow /
   applied / raw serving, fail-safe (exception, timeout, corrupt store,
   configuration problem), station state parsing and listener, recorder
   statistics and history (in-memory recorder), nightly run, switches,
   services, export, diagnostics, flows (menu, hub, groups, options),
   scheduling, example dashboard, ownership and concurrency.
4. **Housekeeping:** 40° South geometry, SRF comment.
5. **Regression:** all 392 tests of 0.1.33.5; one deliberate expectation
   change (recorder exclusion set includes `hours`), two adapted to the 40°
   example (`test_geometry_file_for_the_south_array` declination 40; the 45°
   file keeps its pin to the independent analysis values), tests using
   `hass.data` now use `entry.runtime_data`.

---

## 2. New tests (141)

### 2.1 `test_learning_engine.py` (97)

| Area | Checked against |
|---|---|
| time | DST days with 23/25 local hours; lead buckets at every edge |
| clearness | equals `local_source._hour_clearness` for the same record (one definition); clamp; zero light |
| weather layers | missing layer stays missing (never 0), range 0–100, dew > temperature rejected, °F, size bound, time window, hour = mean of both ends |
| station | ranges (never clamped), global radiation required, 10-min grid ± 2 min, 5 of 6 values, sunshine summed, :00 sample belongs to the previous hour, completeness never decreases, 72 h sample retention |
| predictors | sun ≥ 5° (sunrise hour excluded), all fields required, rounded once to 6 decimals (stored = used) |
| archive | one row per bucket, latest issue wins, elapsed and > 54 h not stored, correction stored with the row |
| analog ensemble | **hand-calculated**: 10 identical analogs → n_eff 10, shrink 0.5, shift = −0.1 for error −0.2; < 8 analogs neutral; pooling of neighbouring buckets; distance limit 1.5 (1.49 / 1.51); K = 25; deterministic ties; bounds; recency 1 / 0.5 / season floor |
| look-ahead | day decided at the cutoff not used; excluded days not used; evidence older than 380 days not used; freeze moves the cutoff; reset sets a floor per scope |
| application | no shift → same object (parity); exact kt/kd shift; instant in kt space; stage-2 bin of the shifted kt; out-of-bounds factor clamped; low sun / no average untouched |
| stage 2 | hand-worked factor 1 − 0.5·n/(n+40); empty bins neutral; excluded days ignored; bounds |
| quality control | ok, station incomplete, snow (albedo 0.6), production missing (waits 3 days), production fault (> 1.2 × kWp, negative), ratio band 0.59/0.61/1.39/1.41, unchecked ratio after waiting; representativeness r = 1 / random / too few hours |
| gate | 14 days pass (RMSE 50 → 0 W/m², MAE 2 → 0 kWh), 13 days fail, 4 % fails, energy worse fails, excluded days ignored, no groups fails; state matrix (8 cases); 06:00 capture once; entry day only same day after 06:00 |
| store | JSON round trip; 17 malformed variants rejected; non-mapping rejected; purge at 399/401 days; event log bound; switch/freeze events; `view()` isolates computations |
| learning | synthetic: low-cloud bias −0.2 recovered within ±0.03 after 40 days, clear hours |shift| < 0.02; neutral start; determinism |
| export | CSV parseable and complete; settings carry every parameter |

### 2.2 `test_learning_hub.py` (34) — real Home Assistant

Setup/entities/ownership; member unchanged while not applied (served value
identical to the entry without a hub) and shadow value visible; entries
outside groups untouched; applied corrections change the served forecast
(> 15 % lower for a −0.2 shift) and the switch refreshes members at once in
both directions; per-group correction sensors; hourly run (archive, 06:00
capture, explanation list); missing cloud layers recorded not raised;
station ingestion and staleness; station time sources (state, attribute,
`age_minutes`); failing and slow corrections → raw (30 s bound, frozen
clock advanced); corrupt store → neutral + event; configuration problem →
paused; switches persist across reload; reset needs confirmation, scopes;
services without hub refused; export folder and retention; diagnostics
without coordinates; location service ignores the hub; unload saves, members
continue; removal keeps the store; model-from-station equals the live model
for the same input; nightly without recorder still completes; flows (menu,
validation of every field, two groups, second hub impossible); hub options
flow; scheduling (startup, xx:25, 03:30); scheduled jobs owned by the entry
and cancelled on unload; owner actions wait for a running job; heavy work
outside the event loop; `PARALLEL_UPDATES` and `runtime_data` ownership;
incomplete days not recorded for the gate; frozen learning keeps the
calibration; example dashboard entity ids exist and the markdown renders.

### 2.3 `test_learning_recorder.py` (2) — in-memory recorder

Nightly run with imported hourly statistics: production read (kWh), model
from station via the member coordinator, day decided with a ratio, gate
evaluated, export written, only days since the store's creation decided.
Station backfill from recorder history (6 samples → 1 hour).

### 2.4 `test_learning_replay.py` (7)

45 simulated days with three issues per day: the replay reproduces
**every** stored correction (max difference ≤ 2·10⁻⁶) without and with a
freeze interval and an owner reset; recomputes the gate's RMSEs within
0.01 W/m²; detects one tampered value; marks rows whose evidence was purged
as not verifiable; rejects a missing export.

### 2.5 `test_shading_examples.py` (+2)

40° South geometry pinned to the accepted refit (±0.01, regression pin; the
independent evidence is the fit against production, release audit); the
45° file keeps its pin to the independent analysis values; both fits agree
within 0.25 at all 17 sun positions.

---

## 3. Defects found in this pass

See release audit §8: D-1 (export retention deleted a non-yearly file —
found by a test), D-2/D-3/D-9/D-10 (test harness), D-4 (sensor class),
D-5 (**race**: gate in a worker thread vs member refreshes) and D-6 (export
on the event loop) — both found in the concurrency review requested by the
owner, D-7 (replay exactness), D-8 (legacy test stand-in), D-11 (optional
values, found by mypy), plus the two test gaps closed after the mutation
run (§4).

---

## 4. Mutation record

**Learning (M109–M190, 82 new):** first run 78/82 caught. Escaped:

| ID | Mutation | Verdict | Action |
|---|---|---|---|
| M117 | sample ending at :00 mapped to the next hour when choosing hours to aggregate | **equivalent**: the aggregation itself always takes the six samples ending at :10…:00; an hour with ≥ 5 values always contains one of its own :10–:50 samples, so it is chosen anyway | documented |
| M120 | predictors created for hours with the sun between 0° and 5° | **gap**: no test had such an hour | `test_hours_with_the_sun_below_five_degrees_get_no_predictors`; caught |
| M124 | rows issued after the cutoff used as evidence | **equivalent** (defensive check kept): rows are archived only before their hour ends and a day is decided only after it ends, so a row of a decided day is always issued before the decision | documented |
| M183 | stage 2 recomputed while frozen | **gap**: freeze tested for stage 1 only | `test_frozen_learning_keeps_the_calibration`; caught |

After the fixes: 80/82 caught + 2 equivalent. M119 was re-run on the final
hub code (entry-owned tasks): caught.

**Earlier mutations (M01–M108) on the final code:** 107/108 caught; M71
(OMSF-012, `AttributeError` not caught for retained data) escapes as
documented since 0.1.33.4 (equivalent: the preceding type checks reject every
input that could raise it). M73 (recorder exclusion) no longer applied
textually because 0.1.34 extended its line; adapted to the new line and
caught. All other 0.1.33.x mutations still apply unchanged and are caught —
including those in `coordinator.py`, `__init__.py`, `config_flow.py` and
`sensor.py`, which this release changed.

The runner now starts with the tests closest to the mutated file
(`--from/--to` added); results: `tests/mutation/mutation_results_0.1.34.json`.

---

## 5. Static type check

`mypy` 2.4 with `mypy.ini` (Python 3.13, HA 2026.2.3 stubs): strict options
(`disallow_untyped_defs`, `warn_return_any`) for `learning`, `learning_hub`,
`learning_entities`, `runtime`, `switch`, `energy`; default checks elsewhere.
Result: 0 findings in new code; the run found 11 kinds of optional-value
misuse in the new code before release (D-11). 38 findings remain in modules
older than 0.1.34 — compared message by message with a run on the 0.1.33.5
tree: identical set. Typical: the library annotates `Estimate.wh_days` keys
as `datetime` but stores `date`; optional coordinator members.

---

## 6. Performance (this container, 2 CPUs, Python 3.13)

| Operation | Time |
|---|---|
| analog corrections for 54 forecast hours, 1 300 evidence rows | ≈ 10 ms |
| corrected estimate per member refresh (second library run) | ≈ 0.1–0.3 s |
| nightly run without recorder data | < 1 s |
| store for one year (estimate) | 1–2 MB |

---

## 7. Gaps

- **No live run yet** on Home Assistant 2026.9.4 with Fusion 0.3.4; the
  station attribute shape is taken from the Fusion notes and the owner's
  paste.
- **The learner's benefit is unproven** until weeks of the owner's data
  exist; the synthetic test proves it recovers a known bias, not that the
  real bias is learnable at the station's distance.
- Recorder interaction tested with the in-memory recorder of the test
  plugin, not MariaDB/PostgreSQL.
- mypy backlog of 38 findings in older modules.
