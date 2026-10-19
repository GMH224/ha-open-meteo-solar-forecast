# Open-Meteo Solar Forecast (GMH224 fork) v0.1.35.0 — Release Audit (ICS)

**Release type:** feature — same-day correction for the learning hub.
**Basis:** owner's battery automations (2026: sunrise + 1 h; 2027: day-ahead with checkpoints), the miss of
19 Oct 2026 (forecast ≈ 20 kWh, actual ≈ 6.5 kWh under a closed deck), the owner's inverter history of 9–19 Oct
(status, DC power, station radiation), the inverter manufacturer's specifications.
**Design record:** `OMSF_v0_1_35_Same_Day_Spec.md`, confirmed 19 Oct 2026 after a mathematical review and a
simulation check; deviations in its §8.
**Invariants:** Open-Meteo mode and entries outside a hub group untouched; nothing is served before the owner's
switch is on and the respective check has passed; any fault leaves the forecast as it would be without the new code.

## 1. Owner decisions (19 Oct 2026)

| Question | Decision |
|---|---|
| Battery decision time | automation moved to sunrise + 1 h (2026 and 2027 versions updated separately) |
| Inverter start-up threshold in the model | not needed: measured, the 5K waits 30 min after sunrise, the 10K never switches off; loss < 0.1 kWh/day |
| Same-day correction acts | only after its own check (7 days, ≥ 5 %, 5 of the last 7 days) and with its switch on |
| Persistence | learned β(Δ) by least squares (recommended over a seventh analog predictor) |
| Cautious bands | `watts_cautious`, 20th percentile, after 10 usable days, with coverage shown |
| Release | 0.1.35.0, usual tests, documentation, audit |

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Engine (new, pure) | clear-sky index (Haurwitz), Erbs split, response curves and inversion, counter bands, signal ê, situations, persistence table β/σ with shrinkage and clamps, corrected and cautious index, light shifts, decision times, check, store section validation, purge, export rows | `same_day.py` |
| Learning engine | sunrise (−0.833°), decision forecast at sunrise + 1 h for the gate (capture and energy half), algorithm 3, store schema 3 with migration 2 → 3, same-day switch and section, view copies the section, settings export | `learning.py` |
| Hub | counter listener and recorder backfill (30 h), 5-minute run (curves hourly, bands, signal, decisions), member interface (plan, report), nightly table and check, switch handling, `same_day_state`/`same_day_applies`, full reset clears the section, export files, `next_hours` total cloud and cell temperature | `learning_hub.py` |
| Coordinator | correction on top of the would-be-served forecast; cautious estimate; band-level model method; report | `coordinator.py` |
| Entities | switch *Same-day correction*; sensors *Same-day correction*, *Same-day improvement*, *Cautious coverage*; `watts_cautious` attribute (capped at `watts`) | `learning_entities.py`, `switch.py`, `sensor.py`, `const.py`, translations |
| Diagnostics | same-day block, counter readings | `diagnostics.py` |
| Replay | recomputes the β/σ table from the export (independent implementation) | `tools/learning_replay.py` |
| Docs | spec 0.1.35, architecture §0e (R42–R48, F28–F32, L-D1–L-D4), README, CHANGELOG, DEVELOPER (module map, rule 14), 0.1.34 spec row 21, example dashboard | |
| Version | 0.1.35.0 | `manifest.json` |

## 3. Mathematical soundness

| Claim | How it is ensured |
|---|---|
| ê measures weather, not array geometry | inversion of the members' own model (all orientations, shading) instead of a production ratio; one k for all groups |
| k_meas and k_fc are comparable | both in the clear-sky index of horizontal light; window forecast weighted by the same clear-sky energy as the measurement |
| β is the least-mean-square predictor | weighted least squares through the origin per lead bucket; includes the attenuation by measurement noise (ρ·σ²/(σ²+σn²)); verified in the suite on synthetic AR(1) data through the integration's own code |
| Few days cannot produce extreme weights | shrinkage toward the 3-h prior with 5 days' weight; β clamped to [0, 1]; own check before acting |
| No look-ahead | table from days strictly before today; check from days before today; decisions recorded at their time (≤ 15 min late), never later |
| Cautious quantile | k̂ − 0.8416·σ (one-sided 80 %), per band; the day sum is at or below its 20th percentile when the band errors are positively correlated; coverage measured on the owner's data |
| Window validity | 0.3 kWh clear-sky minimum derived from the 0.01 kWh counter resolution (|Δk| ≤ 0.033 per counter) |

## 4. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Wrong correction served | own check + switch; shadow until then; bounded shifts; today only; future hours only |
| Stale or failed signal | not used after 15 min; a failed run clears it; members fall back to the forecast without SDC |
| Untrusted counters | unit whitelist, finite and non-negative values, step bound, freshness (`last_reported`), late bands stored as unknown |
| Startup / restart | counter backfill from the recorder; the run waits for every member's first refresh (no failure events) |
| Event loop | response curves rebuilt at most hourly (13 small model runs per member); planning and light shifts in the executor; learning and check in the executor on a consistent view |
| Store integrity | strict validation of the new section; malformed → quarantine as before; migration keeps every measurement |
| Store size | same-day records kept 120 days (≈ 1 MB) |
| Owner reset | `all` clears the section and switches the correction off |

## 5. Verification

See `omsf_v0_1_35_0_ICS_quality_bug_testing_report.md`. Summary: 621 tests passing (+152 subtests); pyflakes clean; mypy: 38 findings, all in pre-0.1.34 modules, identical set (none in the new or changed code); privacy guard passing with `confidential.md` present. Mutations: 260/263 caught + 3 documented equivalent; new M218–M263 46/46 after six test improvements.

## 6. Defects found in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | "Waiting for a member's first refresh" was raised as a step failure every 5 min (event log flooding) | first hub test run (warnings) | returns "waiting", no event; M261 |
| D-2 | `learn_table` used an untyped accumulator list (mypy) | mypy | typed accumulators |
| D-3 | Exactly 5 % improvement failed the check (float rounding) | `test_gate_by_hand` | 1e-9 tolerance, documented (spec §8 row 9) |
| D-4 | Full reset kept the same-day switch | review | switched off and section cleared; test |
| D-6 | The new documents named the inverter brand (private data) | privacy guard `test_public_release.py` with `confidential.md` | generic wording |
| D-7 | Six tests did not discriminate (M219, M230, M231, M253, M255, M258) | mutation testing | tests strengthened, see the test report |
| D-5 | The first design measured ê by bisection on the shifted forecast light, which would have mixed the forecast's diffuse split into the measured k | review while implementing | response-curve inversion (spec §8 row 1) |

## 7. Owner actions

1. Update both battery automations (already done: sunrise + 1 h).
2. Upload 0.1.35.0 (zip), release `v0.1.35.0`, HACS redownload, restart.
3. Events: `migrated` (store 2 → 3) and `algorithm_changed`. The weather learning's count restarts (decision
   forecast).
4. From the first morning: *Same-day correction* shows a value from sunrise + about 1 h; state `collecting`.
   Leave the switch off until *Same-day improvement* has a value and the state says `eligible` (at least 7 usable
   days); then switch on.
5. After 10 usable days `watts_cautious` appears on today's energy sensors; *Cautious coverage* should approach 20 %.
6. Send diagnostics after a few days, ideally including one grey and one clear morning.

## 8. Honest gaps

- Not yet run on the live system. The counter handling is tested with Home Assistant's state machine, not with the
  inverter integration's real update pattern (0.01 kWh steps every few seconds).
- β is learned per lead bucket and two situations; finer conditioning (season, cloud type) needs months of data.
- The response curves use Haurwitz clear sky and Erbs; their bias cancels in ê (both sides use the same curves) but
  not in saturation on very clear days.
- Decision times missed while Home Assistant is down are lost for that day.
