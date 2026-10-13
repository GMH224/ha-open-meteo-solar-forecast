# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34 — Release Audit (ICS)

**Release type:** feature — learning hub (self-learning correction of the
local weather input) + Home Assistant platform hardening.
**Design record:** `OMSF_v0_1_34_Learning_Spec.md` (draft 12 Oct 2026,
confirmed by the owner the same day; §16 answers, §17 deviations).
**Owner decisions:** neutral start, up to 10 weeks of learning acceptable;
one shared store with full learning and telemetry transparency; Tänikon as
the weather reference; learn per inverter; export to
`/config/open_meteo_solar_forecast/export/`; example dashboard; same-day
correction deferred (phase 2); production counters `…dc_input_energy`
(10K: West + South) and `…_2` (5K: East); feed-in unlimited; separate hub
entry; thresholds as proposed; sunrise view 06:00. 13 Oct 2026: implement
`ConfigEntry.runtime_data`, deliberate concurrency (`PARALLEL_UPDATES`,
shared objects, safe database access) and static type safety.
**Invariants:** PV entries outside an inverter group, and all Open-Meteo-mode
entries, behave exactly as in 0.1.33.5; members are unchanged unless the hub
is in state `applied`.
**Verification:** §7 and `omsf_v0_1_34_ICS_quality_bug_testing_report.md`.

---

## 1. Why

The owner's observation (11 Oct 2026): on clear days the forecast is very
good, on cloudy days it is materially wrong. The analysis of 6–11 Oct showed
daily ratios of −21 % … +106 % on cloudy days against ±1 % on clear days,
for all three arrays alike — the error is in the weather input (low stratus
/ fog is where every weather model is weakest), not in the PV physics. Such
errors are systematic for recognisable situations, so they can be learned,
if the truth is measured independently of the PV model. SwissWeather Fusion
0.3.4 now provides that truth: the MeteoSwiss Tänikon pyranometer (8 km;
global and diffuse radiation, 10-minute values), plus cloud cover by layer
in the hourly forecast.

## 2. Method (summary of the design record)

- **Stage 1 — weather (shared):** analog ensemble (Delle Monache 2013,
  Alessandrini 2015). Six predictors: forecast clearness kt, sun elevation,
  low cloud, max(mid, high), temperature − dew point, lead-time bucket
  (0–6, 6–18, 18–30, 30–54 h). K = 25 nearest past hours within distance
  1.5; weights 1/(d + 0.2) × recency (half-life 8 weeks, same season a year
  earlier ≥ 0.5); shift = n_eff/(n_eff + 10) × weighted mean error, bounded
  (Δkt ≤ 0.35, Δkd ≤ 0.5); fewer than 8 analogs → neutral. Applied to
  kt and kd of each hour before the synthesis; the library and all physics
  (transposition, shading, horizon, damping, clipping) run unchanged.
- **Stage 2 — per inverter group:** nightly, the members' own model is
  re-run with measured station light; production ÷ model per measured-kt
  bin (overcast < 0.3, broken, clear ≥ 0.6) over 8 weeks, shrunk
  (n/(n + 40)) and bounded to 0.85–1.15; applied as a factor on the
  corrected light. The clear-bin ratio is reported as "suggested efficiency
  change" rather than hidden.
- **Evidence discipline:** an hour is evidence only after its day passed
  quality control (station completeness ≥ 80 %, no snow, production present
  and plausible, day ratio production ÷ model-from-station in 0.6–1.4); a
  correction uses only evidence decided before its issue time. Station
  representativeness (14-day correlation ≥ 0.85) gates the whole learner.
- **Skill gate:** nightly, over 28 days, on the forecasts recorded at
  06:00: hourly irradiance RMSE vs the station ≥ 5 % better **and** each
  group's daily energy MAE not worse (± 1 %), each with ≥ 14 days. State
  `applied` requires the gate **and** the owner's switch; a later failure
  pauses by itself.

## 3. Changes

| Area | Change | Files |
|---|---|---|
| Learning engine (pure) | parsing (cloud layers, station values), predictors, archive with stored corrections, analog ensemble, application to hour records, stage 2, quality control, representativeness, gate, state, strict store (de)serialisation, retention, export text | `learning.py` (new) |
| HA glue | hub: store + quarantine, station listener and recorder backfill, hourly (xx:25) and nightly (03:30) runs, recorder statistics, model-from-station via member coordinators, coordinator interface, export files, services | `learning_hub.py` (new) |
| Entities | 7 hub sensors, 4 per group, 2 switches | `learning_entities.py`, `switch.py` (new), `sensor.py` |
| Coordinator | raw and corrected estimate side by side for group members; corrected served only in `applied`; 30 s bound; `async_model_hours`; `learning` attribute | `coordinator.py` |
| Configuration | entry type "learning hub" (menu once a PV entry exists, unique), station page, group pages; options flow for the hub; validations | `config_flow.py`, `const.py`, strings EN/DE |
| Services | `reset_learning` (confirm + scope), `export_learning` | `__init__.py`, `services.yaml` |
| HA platform | `runtime_data` ownership, typed aliases and models, coordinator `config_entry`, `PARALLEL_UPDATES`, lookups of loaded entries only | `runtime.py` (new), `__init__.py`, `energy.py`, `diagnostics.py`, `sensor.py`, `switch.py` |
| Transparency | hub diagnostics, export, replay tool, example dashboard | `diagnostics.py`, `tools/learning_replay.py`, `examples/dashboard/learning.yaml` |
| Housekeeping | South geometry refitted for 40°; 45° kept; SRF comment | `examples/shading/*`, `local_provider.py` |
| Type check | `mypy.ini` | new |

## 4. Behaviour and parity

- **Not a member / no hub / Open-Meteo mode:** identical to 0.1.33.5
  (`test_entry_without_hub_or_group_behaves_as_before`; the whole 0.1.33.5
  suite passes unchanged except one deliberate expectation: the recorder
  exclusion set now contains `hours`).
- **Member, hub not `applied`:** the raw estimate is served; the corrected
  one is computed and shown ("shadow"); the served values are identical to
  0.1.33.5 (`test_member_forecast_is_unchanged_while_not_applied`). Cost:
  one more library run per refresh (local data only, a few hundred ms).
- **Member, `applied`:** corrected estimate served; raw totals remain
  visible in `forecast_source.learning` and the group sensors.
- **Switch off / gate fails:** members are refreshed at once and serve raw.
- **Hub removed:** learning store and export are kept (a new hub continues);
  `reset_learning` scope `all` deletes the learned state.
- **Upgrade:** no migration; existing entries keep their options and their
  retained-forecast fingerprint.

## 5. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Forecast availability | learner faults never propagate: hub methods catch, the coordinator catches and bounds the corrected run to 30 s, outside the local-source timeout; raw forecast served (tests + M171–M173, M178) |
| Authority | corrections act only with gate passed **and** owner switch on; reset needs `confirm: true` |
| Bounded influence | Δkt ≤ 0.35, kd ∈ [0.05, 1], stage 2 ∈ [0.85, 1.15], stored factors re-validated on load and clamped on use; hours > 54 h never corrected |
| Look-ahead / self-deception | evidence only after its day's decision, before the issue time; gate on recorded 06:00 forecasts (true out-of-sample); replay tool reproduces every stored correction independently |
| Untrusted input | station values range-checked, never clamped or filled; cloud layers 0–100, missing = no forecast; dew point > temperature rejected; series size and time window bounds reused; production counter faults detected |
| Store | strict schema validation; malformed → copy kept, neutral start, event; size ≤ 8 MB (oldest dropped, event); retention 400 days |
| Files | export only to a fixed folder; names built from constants/years/dates; atomic writes; retention deletes only the module's own yearly files (D-1) |
| Resources | all computation in the executor; analog search vectorised (< 50 ms per run); export built in the executor |
| Concurrency | one hub lock for runs and owner actions; executor jobs on `LearningData.view()`; to_json in the loop; recorder via its own executor; `PARALLEL_UPDATES` 0 (sensors) / 1 (switches) |
| Lifecycle | `runtime_data` owns coordinator / hub; listeners and timers removed on unload; store written on unload; cross-entry lookups only for loaded entries |
| Information exposure | diagnostics without coordinates or paths; error text through `sanitize_error`; the per-hour explanation list is excluded from the recorder |
| Network / supply chain | none added (numpy, PyYAML ship with HA) |

## 6. Home Assistant platform review (owner request, 13 Oct 2026)

Before this release none of the three practices was implemented:

| Practice | Before | Now |
|---|---|---|
| Explicit lifecycle ownership | coordinator and hub in `hass.data` (global dict), popped manually on unload | `ConfigEntry.runtime_data` owns the coordinator / hub; the coordinator receives `config_entry`; nothing in `hass.data` (`test_platform_concurrency_and_runtime_ownership`) |
| Deliberate concurrency | no `PARALLEL_UPDATES`; the review found two real defects in the new hub code (D-5, D-6) | `PARALLEL_UPDATES` 0/1; owner actions serialised with the runs (`test_owner_actions_wait_for_a_running_job`); executor jobs on consistent views (`test_view_isolates_*`, `test_heavy_work_runs_outside_the_event_loop`); recorder only through `get_instance(hass).async_add_executor_job`; one shared hub for all members, one coordinator per entry |
| Static type safety | coordinator typed; entries, runtime objects and stored dicts untyped; no type check | typed entry aliases (`ArrayConfigEntry`, `HubConfigEntry`), TypedDicts (`GroupConfig`, `LearningReport`, `EntryReport`); mypy (`mypy.ini`): new modules clean under strict options; the run found and fixed optional-value defects in the new code (D-11); the 38 findings in older modules are exactly those of 0.1.33.5 (verified set-equal), recorded as backlog |

## 7. Verification

See the test report. Suite: 533 tests passing (+152 subtests), pyflakes
clean, mypy without new findings. Mutation record: 187/190 caught on the
final code + 3 documented equivalent mutants (test report §4).

## 8. Defects in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | Export retention deleted `forecast_0600.csv` ("0600" read as a year) | `test_export_writes_the_fixed_folder` | only the module's yearly prefixes are cleaned; regression mutation M181 |
| D-2 | In-memory recorder tests could not start (an autouse fixture set up HA first) | first run | separate module `test_learning_recorder.py` |
| D-3 | Timeout test hung (frozen test clock) | first run | test advances the clock while "working" |
| D-4 | Energy-error sensor: device class energy with state class measurement (HA warning) | HA log in tests | device class removed |
| D-5 | **Race:** the nightly gate iterated the entry-day records in a worker thread while member refreshes add to them | concurrency review (owner request) | executor jobs receive `LearningData.view()` |
| D-6 | Export CSVs were built on the event loop (≈ 100 ms per night) | same review | built and written in the executor |
| D-7 | Replay could differ in the 6th decimal (unrounded predictors used, rounded stored) and after the first year (evidence beyond retention) | designing the replay check | predictors rounded once; evidence ≤ 380 days |
| D-8 | Legacy wizard test (minimal hass stand-in) failed on the new menu | full suite | tolerant entry lookup |
| D-9 | Flow test added an entry after the form was rendered | first run | test order |
| D-10 | Station test fed 80-minute-old states (rejected as designed) | first run | test advances the clock |
| D-11 | Optional values used without a check in the new code (hour records, gate MAE, member lookups, service response) | mypy | explicit checks |
| M-1 | Two test gaps: an hour with the sun between 0° and 5° (M120) and stage 2 while frozen (M183) | mutation run | two tests; both caught |
| P-1 | Process: the draft spec proposed a 05:00 sunrise view and a curtailment sensor; the owner chose 06:00 and has unlimited feed-in | owner review | spec §16/§17 |

## 9. Gaps and limitations

- **No live run yet** on the owner's Home Assistant 2026.9.4 with Fusion
  0.3.4; verified on 2026.2.3 (test harness) with Fusion-shaped fixtures.
- **Station attribute shape** taken from the Fusion 0.3.4 notes and the
  owner's paste (state = measurement time, `values` with MeteoSwiss short
  names); fallbacks exist (time attribute, `age_minutes`); the `station`
  attribute of the state sensor shows which one is used.
- **The learning value is unproven** until weeks of data exist; the gate
  makes that explicit — the hub may stay in `shadow` for a long time.
- Limitations L-L1 … L-L5 (architecture §0d).
- mypy: 38 findings in pre-0.1.34 modules (backlog, unchanged).

## 10. Owner actions

1. Commit, tag `v0.1.34`, GitHub release (no asset), HACS redownload,
   restart.
2. Keep the 40° configuration; the South shading file in the repository is
   now the 40° version (your `/config/shading/south_geometry.yaml` already is).
3. Add the hub: *Add integration → Open-Meteo Solar Forecast → Set up the
   learning hub*; station `sensor.swissweather_fusion_reference_station`;
   group `10K`: `sensor.inverter_total_dc_input_energy`, members West and
   South; group `5K`: `sensor.inverter_total_dc_input_energy_2`, member
   East.
4. Leave **Apply learning off**. Check after a day: state `collecting`,
   `next_hours` filled, `last_run` without failed steps, files in the export
   folder.
5. Optional: paste `examples/dashboard/learning.yaml` into a new dashboard.
6. When the state becomes `eligible` (expected after 3–6 weeks for autumn
   situations), look at the improvement and the energy error, then decide
   on the switch.
