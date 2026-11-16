# Open-Meteo Solar Forecast (GMH224 fork) v0.1.36.0 — Release Audit (ICS)

**Release type:** feature — measured light at the house as a sensor for another integration; one same-day fix.
**Basis:** review of 16 Nov 2026 (owner's export, 20 Oct – 15 Nov); proposal to and response from the weather
provider's team (16 Nov); owner decisions of 16 Nov.
**Design record:** `OMSF_v0_1_36_House_GHI_Spec.md` (decisions and deviations in §10); interface
`docs/house_ghi_interface.md`; architecture §0g (R52–R56, F33–F36).
**Invariants:** nothing served to the PV forecasts changes, except that a valid same-day signal reaches the
members up to 10 minutes earlier; Open-Meteo mode and entries outside a hub group untouched; no store schema
change.

## 1. Owner and consumer decisions (16 Nov 2026)

| Question | Decision |
|---|---|
| Build the sensor | yes, now; the consumer records it first and decides on use in Feb–Mar 2027 |
| Quantity | GHI, W/m², flat and unobstructed; no PV geometry for the consumer |
| Window | 15 minutes, energy over the window |
| Quality | good / uncertain (published with reason) / bad (`unavailable`, with reason) |
| Export limit (curtailment) | none at this installation: reason kept in the interface, never detected |
| Same-day fix A (fresh signal before the decision) | included |
| Low-sun limits | bad < 10°, uncertain 10–18° (owner's clear-day data) |
| Consumer requests | no forecast in the value; daily bad counts by reason; full history in the export; fixed references with recorded version; uncertain values published |

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Engine (new, pure) | window clear-sky GHI and elevation, quality rules, snow verdict, hour value, day counts, sensor attributes, store section (validation, version, retention), export rows | `house_ghi.py` |
| Hub | house step after every 5-minute run and at :02/:17/:32/:47; response curves with measured temperature; inverter limits; snow from the station; recent windows for the sensor; export files; full reset keeps the windows; member refresh on a valid same-day signal; `signal_time` | `learning_hub.py` |
| Store | section `house_ghi` (validated, copied in `view()`, purged with the 760-day retention); settings export | `learning.py` |
| Entities | sensor *House light (measured)* | `learning_entities.py`, translations |
| Diagnostics | block `house_ghi`, window count | `diagnostics.py` |
| Docs | spec 0.1.36, interface document, architecture §0g, README, CHANGELOG, DEVELOPER (module map, rule 16) | |
| Version | 0.1.36.0 | `manifest.json` |

## 3. Soundness

| Claim | How it is ensured |
|---|---|
| The value is horizontal light, not PV output | inversion of the members' own model on clear-sky-shaped light; orientation and shading drop out |
| No forecast and no learning in it | separate curves: synthetic light (`curve_records`), measured station temperature; no stage-1/stage-2/same-day input; test checks the model inputs and that switching on every learned correction leaves a window unchanged (M289) |
| Clear-sky reference is right | Haurwitz mean of 5 instants, checked by hand and against pvlib (1 %); elevation against pvlib (0.1°) |
| Doubtful values are flagged | each rule tested at its edge; bad windows carry no GHI (validation, M287) and keep the sensor unavailable (M285, M296) |
| Plausible on real data | the same k from the owner's same-day bands, hourly vs the reference station 8 km away: ratio 0.93, r = 0.91, mean abs. difference 40 W/m² at sun ≥ 18°; 0.81 / 0.89 at 10–18° (expected: obstacles at low sun) |
| Fix A serves what is measured | test: members without a signal, a run with a valid signal → members refreshed and the decision records the corrected remaining energy (M294) |

## 4. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Consumer learns from its own output | no forecast input (R52); documented in the interface; tested |
| Bad data published | quality rules; `unavailable` when bad; stale (> 45 min) → unavailable |
| Missing counters | wait ≤ 30 min, then bad `counter`; never zero (M276) |
| Station temperature missing | 10 °C assumed, quality uncertain |
| Event loop load | 13 model runs per member per hour (as the same-day curves); assessment of a few windows per run; sensor reads only the last 36 h of windows |
| Member refresh hangs (fix A) | bounded to 60 s; decisions recorded with the reports at hand |
| Store size | ≈ 1.5 MB per year, 760-day retention; covered by the existing 16 MB guard |
| History loss | own store and export, independent of the recorder; survives a full reset |
| Method change unnoticed | `VERSION` with date in the store, export and attributes |

## 5. Verification

See `omsf_v0_1_36_0_ICS_quality_bug_testing_report.md`. Summary: 655 tests passing (+152 subtests), stable over
repeated runs; pyflakes clean; mypy 38 findings, the known set in pre-0.1.34 modules (none in new or changed
code); privacy guard passing with `confidential.md` present. Mutations: new M271–M299 29/29 caught; regression
on every mutant in a changed module (M109–M270 in `learning.py`, `learning_hub.py`, `learning_entities.py`, plus
M255 and M259–M261 whose tests were edited): 114/116 caught + 2 documented equivalent (M117, M124), no regression.

## 6. Defects found in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | same-day staleness test set the old field (`last_same_day["signal_time"]`) | suite after introducing `signal_time` | test uses `hub.signal_time` |
| D-2 | hub tests were sensitive to runs started by timers while the test clock jumps (flaky failures in parallel runs, one in five for the fix-A test) | repeated runs | the shared test setup waits for background runs; two tests clear what a timer recorded |
| D-3 | engine test did not isolate "bad window published" (a bad window never had a GHI in the test) | mutation M285 first caught elsewhere | explicit case: a bad window with a value is not published |
| D-4 | proposal judged mixed sky on k; on grey days (k ≈ 0.3) that would mark diffuse-only light as uncertain | design review | rule on the Erbs diffuse fraction (spec §10 row 1) |

## 7. Owner actions

1. Release `v0.1.36.0` (tag), HACS redownload, restart.
2. Check `sensor.solar_forecast_learning_house_ghi`: a value from about sunrise + 30–60 min; in the morning mostly
   *uncertain* (`low_sun`). Event `house_ghi_version` once.
3. Give the weather team the entity id and `docs/house_ghi_interface.md`; after a few days, send them
   `house_ghi.csv` from the export folder (their request for a first example).

## 8. Honest gaps

- Not yet run on the live system; the inverters' real reporting pattern is represented in tests by state changes.
- The low-sun rules are fixed limits; obstacles at low sun are not learned yet (planned, version 2).
- Snow detection needs a bright station sky; snow on a grey day stays undetected until the sky clears (then the
  house is far darker than the station).
- Curtailment is not detectable; correct for this installation (no export limit), not in general.
