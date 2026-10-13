# Open-Meteo Solar Forecast (GMH224 fork) — Learning Specification (draft for 0.1.34)

**Status:** IMPLEMENTED in 0.1.34. Confirmed by the owner on 12 Oct 2026
(answers in §16). Where the implementation deviates from or refines the
draft, §17 says so; §17 wins over the earlier sections.
**Date:** 12 October 2026
**Owner decisions so far:**
- start neutral; learning may take up to 10 weeks;
- one shared store ("B") with full learning and telemetry transparency;
- the MeteoSwiss station Tänikon (via SwissWeather Fusion ≥ 0.3.4) serves as the weather reference;
- learn production per inverter;
- export to `/config/open_meteo_solar_forecast/export/` (folder already created);
- include an example dashboard;
- the same-day correction is phase 2 (§12).

---

## 1. Goal and non-goals

**Goal.** Reduce the error of the forecast the owner reads at sunrise
(today and tomorrow) on cloudy days. The October 2026 analysis attributed
this error mainly to the weather forecast (low stratus/fog). The physics
(geometry, shading, efficiency) is already good to a few percent on clear
days.

**Principle.** Physics stays as it is. The learner corrects only what the
physics cannot know:
1. **Stage 1 — weather:** how much light actually arrives in situations the
   weather forecast describes in a certain way (e.g. "40 % low cloud, small
   temperature–dew point spread, ICON says kt 0.55"). Learned against the
   Tänikon pyranometer, shared by all three arrays.
2. **Stage 2 — per inverter:** a small residual calibration of the PV model
   (e.g. low-light losses on overcast hours), learned from production against
   the model driven by **measured** Tänikon light.

**Non-goals (this release).**
- No black-box model of PV power from raw inputs.
- No correction of Open-Meteo days. In hybrid mode only the days from
  Fusion are corrected; days beyond the Fusion horizon are untouched.
- No change to anything while the owner's switch is off.
- No same-day correction (that is phase 2, §12).
- No network access.

## 2. Why this method (analog ensemble on forecast errors)

| Option considered | Verdict |
|---|---|
| Global bias factor | Wrong in both directions (Oct week: −21 % … +106 %); averages cancel |
| Regression / gradient boosting / neural net | Needs thousands of cases per situation; opaque; hard to verify |
| **Analog ensemble** (Delle Monache 2013; Alessandrini 2015 for solar) | Works from a few hundred hours; every correction is explained by the list of past hours it came from; degrades to neutral when no similar past exists |

**How it works.** For each future hour:
1. Find the K most similar *past forecast situations* from the archive.
2. Look at how wrong the forecast was in those situations. Error = measured − forecast clearness index kt.
3. Shift the new forecast by their weighted mean error. The shift shrinks toward 0 when there are few or dissimilar past hours.

Because what is learned is the **error**, not the value, the learner starts
neutral (shift 0) by construction.

## 3. Data sources and time semantics (all UTC)

| Data | Source | Semantics | Use |
|---|---|---|---|
| Forecast irradiance GHI/DHI (hour average) | Fusion `hourly_forecast` (already used) | average over `[period_start, period_end)` | predictor kt_f, kd_f; corrected output |
| Cloud cover low / mid / high | `weather.get_forecasts` hourly (Fusion ≥ 0.3.4) | instant at `datetime`; missing beyond ~5 days = no data | predictors |
| Temperature, dew point | same | instant at `datetime` | predictor: spread T − Td (fog/stratus indicator) |
| Measured GHI, DHI, sunshine, sky longwave, reflected shortwave | `sensor.swissweather_fusion_reference_station`, attribute `values` (`gre000z0`, `ods000z0`, `sre000z0`, `oli000z0`, `osr000z0`, `tre200s0`) | 10-min values **ending** at the station time | Stage 1 truth; Stage 2 driver; flags |
| Production | DC input energy counter per inverter (owner: presumably `sensor.inverter_total_dc_input_energy` = 10K West+South, `…_2` = 5K East — **to confirm**) | recorder long-term statistics, hourly `sum` change | Stage 2 truth; skill gate |

**Hour alignment.**
- **Forecast hour `[h, h+1)`:**
  - GHI and DHI are the series value with `period_start = h`.
  - Cloud cover, temperature and dew point are the mean of the instants at h and h+1. If only one instant exists, that one is used.
- **Measured hour `[h, h+1)`:**
  - The mean of the six 10-min values stamped h+0:10 … h+1:00.
  - At least 5 of the 6 values are required, otherwise the hour is missing. Nulls are never filled.
- **kt (clearness index):** hour GHI ÷ hour mean of E0·cos Z. This uses the existing
  `_hour_clearness` machinery in `local_source.py`, so no clear-sky
  turbidity model is introduced.
- **kd (diffuse fraction):** DHI ÷ GHI.
- **Hours learned from:** only hours with mean sun elevation ≥ 5°.

## 4. Predictors (6) and distance

| # | Predictor | Scale (1 unit of distance) |
|---|---|---|
| 1 | forecast kt_f | 0.10 |
| 2 | sun elevation (hour mean) | 10° |
| 3 | low cloud | 25 % |
| 4 | max(mid, high) cloud | 30 % |
| 5 | T − Td spread | 2 K |
| 6 | lead time bucket | *filter, not distance*: `0–6 h`, `6–18 h`, `18–30 h`, `30–54 h` |

- **Distance** d = √Σ((xᵢ − yᵢ)/scaleᵢ)².
- **Candidates:** only past hours in the same lead bucket. If there are fewer than 8 candidates within d ≤ 1.5, the two neighbouring buckets are pooled and a flag `pooled` is set.
- **UV index:** dropped (redundant with kt_f and elevation).
- **Missing cloud layers:** if a forecast hour has no layer data (beyond the ICON horizon), it gets no correction.

**Weights.**
- Per analog: w = 1/(d + 0.2) · recency.
- **Recency:** a half-life of 8 weeks. Hours from the same season one year earlier (±30 days of day-of-year) get a recency floor of 0.5, so last year counts next autumn.
- **Neighbours:** K = 25 nearest.

## 5. Stage 1 — weather correction

For a forecast hour with predictors x:

- **Raw learned shift:**
  - Δkt = Σw·(kt_meas − kt_f) / Σw over the K analogs.
  - Δkd the same, using kd_meas − kd_f.
- **Shrinkage:**
  - n_eff = (Σw)² / Σw². The applied shift is s·Δ, with s = n_eff/(n_eff + 10).
  - With 3 good analogs s ≈ 0.23; with 25 it is ≈ 0.7.
  - If there are fewer than 8 analogs in total, s = 0 (neutral).
- **Bounds:**
  - |s·Δkt| ≤ 0.35.
  - kt′ = clamp(kt_f + s·Δkt, 0, 1).
  - kd′ = clamp(kd_f + s·Δkd, 0.05, 1).
- **Output:**
  - GHI′ = kt′·⟨E0·cos Z⟩ and DHI′ = kd′·GHI′.
  - DNI′ is derived as today (hour average and instant anchors get the same Δkt, Δkd).
- **Where it is applied:** to the parsed `HourRecord` list **before**
  `build_open_meteo_payload` (pure function, new module `learning.py`).
  Transposition, shading, horizon, temperature, damping and inverter
  clamping then run unchanged.

**Effect.** All three entries read the same Fusion series, so all three get
the same corrected weather. The corrected diffuse fraction matters: on
cloudy hours more diffuse light means the South shading and the plane
orientation matter less, which the physics then handles correctly.

## 6. Stage 2 — per-inverter calibration

**Driver.**
1. Each night, the model is re-run for yesterday with **measured** Tänikon
   GHI/DHI instead of the forecast. These are the same entries, settings and
   library, in local mode with a synthetic `HourRecord` list.
2. The result is summed per inverter group: 10K = West + South, 5K = East.

**Learned factor.**
- c_g(b) = Σ measured DC energy / Σ modelled energy over the last 8 weeks.
- Measured kt bins b: `< 0.3` overcast, `0.3–0.6` broken, `> 0.6` clear.
- Only hours with elevation ≥ 10° count.

**Shrinkage and bounds.**
- The applied factor is 1 + (c − 1)·n/(n + 40), with n = hours in the bin.
- It is bounded to 0.85…1.15.
- It is applied to the power (W and Wh) of every entry in the group, for the
  bin of the hour's corrected kt′.

**Why per bin.** On clear hours the factor should be ≈ 1; the efficiency
settings were fitted there. A factor ≠ 1 on overcast hours measures what the
physics misses at low light (IAM, low-irradiance efficiency, spectral).
Those are exactly the effects the owner suspected.

**Report, not hidden.** The clear-sky bin factor is *also* shown as
"suggested efficiency change". If it settles away from 1 (e.g. 0.96), the
owner should change the efficiency setting rather than let the learner
compensate forever.

## 7. Quality controls (what never becomes evidence)

A day or hour is excluded from learning, with a logged reason, when:

| Reason | Rule |
|---|---|
| Station data missing | < 5 of 6 values in the hour |
| Station not representative of the house | day ratio PV ÷ model-from-station outside 0.6–1.4 (local fog boundary, outage) |
| Station correlation degraded | rolling 14-day hourly correlation PV vs station < 0.85 → learning **paused**, state `paused: station` |
| Snow | reflected ÷ global (`osr000z0`/`gre000z0`) > 0.4 with global > 100 W/m² → day excluded (snow on modules unknown) |
| Production counter fault | negative step, step > 1.2 × group kWp × 1 h, or missing statistic |
| Curtailment | hours flagged by an optional "export limited" binary sensor (owner to say whether this ever happens) |
| Forecast missing | no archived forecast for the bucket |

## 8. Shared store ("B"), retention and size

- **File:** one HA `Store`, `.storage/open_meteo_solar_forecast.learning`. The JSON is
  versioned and written with HA's atomic save, delayed 60 s.
- **Contents:**
  - **Forecast archive:** per target hour and lead bucket, the latest
    forecast issued in that bucket (issue time, kt_f, kd_f, GHI_f, low, mid,
    high, T, Td).
  - **Station hours:** GHI, DHI, sunshine, sky longwave, reflected,
    temperature, number of values.
  - **Production hours** per group.
  - **Stage 2 model-from-station hours** per group.
  - **Event log:** ring of 500 entries.
  - **Model state:** algorithm version, frozen flag, gate state, Stage 2
    factors, last run.
- **Retention:** 400 days (one full year plus margin). Expected size ≈ 1–2 MB; hard limit
  8 MB. Beyond it, the oldest hours are dropped and an event is logged.
- **Corrupt or unreadable store:**
  1. The file is copied to `…learning.corrupt-<date>`.
  2. The learner starts neutral and logs an event.
  3. The forecast is never blocked.
- **Backfill on first start:**
  - Production: from the recorder's long-term statistics. A year exists, so Stage 2 can see the past as soon as station data exist.
  - Station: from recorder state history (≈ 10 days).
  - Forecasts: none possible. They were never stored, which is why the archive starts now.

## 9. Control: the Learning hub and the owner's switch

**Hub entry.** A new config-entry type, "Learning hub", added from the same
integration (the dialog offers *PV array* or *Learning hub*; at most one
hub). The hub holds the shared settings:

| Setting | Default |
|---|---|
| Reference station sensor | auto-discovered `sensor.swissweather_fusion_reference_station` |
| Weather entity / radiation sensor | taken from the array entries (must agree; otherwise a repair issue) |
| Inverter groups | name + DC energy sensor + member entries (10K: West, South; 5K: East) |
| Optional curtailment sensor | none |

Each array entry gets one new option, **Inverter group** (empty = not
learning). Entries without the hub behave exactly as 0.1.33.5 (parity).

**States.**

| State | Meaning | Forecast |
|---|---|---|
| `collecting` | < 14 days with complete data | raw |
| `shadow` | gate not (yet) passed; corrections computed and shown, not applied | raw |
| `eligible` | gate passed, switch off | raw |
| `applied` | gate passed, switch on | corrected |
| `paused` | gate failed again, station unrepresentative, or store fault — reason shown | raw |
| `frozen` | owner froze learning: corrections still applied if the switch is on, evidence no longer updates the model | as before freeze |

**Skill gate.** Every night, out of sample, the learner compares raw and
corrected forecasts for the last 28 days, with at least 14 complete days.
Each hour's correction uses only data older than its issue time, so there is
no peeking. The gate passes when:
1. hourly GHI RMSE vs Tänikon (lead bucket of the 05:00-local forecast, i.e.
   the sunrise view) improves by ≥ 5 %, **and**
2. daily energy MAE per inverter group is not worse (± 1 %).

**Automatic suspension.** If a nightly gate fails while `applied`, the
learner falls back to raw: the state becomes `paused`, an event is logged,
and the switch stays on. It resumes automatically when the gate passes
again.

**Owner controls.**
- switch *Apply learning*: default **off**;
- switch *Freeze learning*;
- service `open_meteo_solar_forecast.reset_learning` with a mandatory
  `confirm: true` and a `scope` of `stage1`, `stage2` or `all`. The archive is
  kept unless `scope: all`;
- service `…export_learning` for an export on demand.

## 10. Transparency

**Hub sensors.** All have a `state_class`, so they get long-term statistics.

| Sensor | Content |
|---|---|
| Learning state | state as in §9; attributes: reason, algorithm version, since |
| Evidence | hours usable; days complete; per lead bucket; attributes: exclusions by reason (last 28 d) |
| Skill — irradiance | RMSE raw / corrected (W/m², 28 d), improvement % |
| Skill — energy per group | daily MAE raw / corrected (kWh, 28 d) |
| Correction today / tomorrow per group | kWh corrected − raw (sign shows direction) |
| Next hours | attributes: per hour for the next 36 h — predictors, kt_f → kt′, kd_f → kd′, n analogs, n_eff, shrink s, pooled flag, top 5 analog dates with their error. **This is the "why" of every correction** |
| Stage 2 factors per group | three bins; attribute "suggested efficiency change" |
| Station representativeness | 14-day correlation, last excluded day and reason |
| Snow flag (Tänikon) | on/off with ratio |
| Last learning run | timestamp; duration; outcome |

**Array entries.** The existing attribute `forecast_source` gets a block
`learning`: applied yes/no, state, energy today/tomorrow raw vs corrected.
This means the raw forecast is always visible next to the corrected one.

**Diagnostics** (hub): settings, state, gate history (90 nights), event log,
store size, counts per exclusion reason. No file paths or entity attributes
beyond entity IDs.

**Export** (nightly 03:30 local, and on demand) to
`/config/open_meteo_solar_forecast/export/`:

| File | Rows |
|---|---|
| `hours_<year>.csv` | one per target hour × lead bucket: issue time, predictors, forecast, correction, measured station values, production per group, model-from-station per group, exclusion reason |
| `daily.csv` | one per day and group: raw / corrected / actual energy, gate inputs |
| `events.csv` | the event log |
| `snapshots/model_<date>.json` | weekly: full model state |

- **Path:** fixed (not configurable, so no path input to validate).
- **Retention:** files older than 400 days are deleted.
- **Bounds:** size bounded by the retention.
- **Replay tool:** `tools/learning_replay.py` recomputes every correction
  from the export. It is the independent check that the integration does what
  this document says.

**Example dashboard** (`examples/dashboard/learning.yaml`). Core cards
only, no custom cards:
1. a statistics graph of raw vs corrected vs actual daily energy per group;
2. the skill trend;
3. a markdown card listing the next hours' corrections with their reasons;
4. the state and switches;
5. station vs production for today.

## 11. ICS considerations

| Concern | Measure |
|---|---|
| Fail-safe | every learner fault → raw forecast (pre-0.1.34 behaviour); forecast never blocked or unavailable because of learning |
| Bounded influence | Δkt ≤ 0.35; kd within 0.05–1; Stage 2 within 0.85–1.15; Open-Meteo days untouched |
| Owner authority | applied only with the owner's switch **and** a passed gate; freeze and reset with confirmation |
| Determinism and auditability | same store → same result; algorithm version recorded; replay tool; event log |
| Input validation | all values finite and in physical ranges; station age ≤ 60 min; units checked; counter resets detected |
| Resource bounds | store ≤ 8 MB; analog search ≈ 6 000 × 54 hours in numpy, < 50 ms, in the executor; export bounded by retention |
| Event loop | all computation and file writes in the executor |
| Change control | algorithm version change → gate state reset to `shadow` until it passes again with the new version |
| Network / dependencies | none added (numpy ships with HA) |

## 12. Phase 2 (not in 0.1.34): same-day correction

Once the day has started, Tänikon shows how wrong this morning's forecast
is. The ratio measured ÷ forecast of the last hour is applied to the next
hour and fades to 1 over about 3 hours. Fog can still lift, and beyond about
2 hours the weather model is better than persistence again. The remaining-day
energy updates during the day.

- **Value:** for automations during the day.
- **No value:** for the sunrise view.
- **Gating:** the same gate, switch and transparency pattern.

## 13. Expected learning curve (honest estimate)

| After | Expected |
|---|---|
| 0–2 weeks | `collecting`; shadow corrections visible but noisy |
| 3–6 weeks | gate can pass for common autumn situations (fog/stratus, broken cloud) |
| 10 weeks | broad coverage of the season's situations |
| Spring | new sun elevations: corrections shrink toward neutral automatically until evidence exists; last year's autumn does not transfer to spring |

No gain should be expected on clear days; the physics already does that.
The learner's value is on the days it learns are systematically misjudged.

## 14. Verification plan (for the release)

- **Unit tests:** hour alignment (station end-stamps, forecast instants);
  distance and weights; shrinkage (neutral with < 8 analogs); bounds; all
  exclusion rules; gate (no peeking: corrections use only data older than
  their issue time).
- **Synthetic learning test:** a known bias is injected in low-cloud
  situations. The learner must recover it within ±0.03 kt after N
  days, and leave clear hours unchanged.
- **Parity:** without a hub or without a group, bit-identical to 0.1.33.5.
  With the switch off, bit-identical too.
- **HA integration:** hub dialog, groups, switch, services, repair issues,
  store corruption, restart backfill, export files.
- **Replay:** the tool reproduces the integration's corrections exactly.
- **Mutation testing:** gate, shrinkage, bounds, alignment, exclusions,
  fail-safe.
- **Retrospective check on the owner's data:** the 6–11 Oct week replayed once
  enough archive exists.

## 15. Also in 0.1.34 (housekeeping)

- `examples/shading/south_geometry.yaml` replaced by the 40° fit; the test
  pinning it to the analysis model is updated.
- Comment fix in `local_provider._comparison`: the SRF hour averaging is
  documented by Fusion 0.3.4, not "undocumented".

## 16. Owner answers (12 Oct 2026)

1. **Production sensors:** `sensor.inverter_total_dc_input_energy` = 10K
   (West PV1 + South PV2), `sensor.inverter_total_dc_input_energy_2` = 5K
   (East). Confirmed.
2. **Curtailment:** feed-in is unlimited, so there is no curtailment to
   detect; the optional curtailment sensor is dropped (§17).
3. **Hub:** a separate "Learning hub" entry is OK.
4. **Gate thresholds:** accepted as proposed.
5. **Sunrise view:** 06:00 local.

## 17. Implementation decisions and deviations (0.1.34)

| # | Draft | Implemented | Why |
|---|---|---|---|
| 1 | each array entry gets an option "Inverter group" | group membership is chosen **in the hub** (group page: name, energy counter, member entries) | array entries stay bit-identical; one place to configure; validated together (each member once, all members local/hybrid, same weather source) |
| 2 | gate on the 05:00 forecast | **06:00 local** | owner (§16) |
| 3 | optional curtailment sensor | dropped | feed-in unlimited (§16) |
| 4 | state `frozen` | states `collecting`, `shadow`, `eligible`, `applied`, `paused`; *frozen* is a switch shown as an attribute | freezing changes what the model learns, not whether it acts |
| 5 | — | hours with a lead time beyond 54 h are never corrected (in hybrid mode Fusion days 3–5 stay raw) | no archive bucket, no evidence |
| 6 | — | a correction is stored with each archived row at issue time; the forecast uses the latest row of each hour (also for today's elapsed hours) | the forecast, the export and the replay use the same numbers |
| 7 | — | predictors rounded once to 6 decimals; evidence older than 380 days not used (retention 400 days) | the replay tool can reproduce every recent correction **exactly** from the export |
| 8 | — | shrinkage n_eff/(n_eff+10) with K = 25 saturates at ≈ 0.71 | by design: at most ≈ 70 % of a well-known bias is applied |
| 9 | stage 2 "applied to the power" | applied as a factor on the corrected irradiance before the library | the library stays the single power model (clipping, day totals and 15-minute values stay consistent); cell temperature changes slightly with it |
| 10 | — | stage 2 uses the members' own model (same settings, shading, horizon) re-run on measured station light, nightly, last 10 days | the factor measures exactly what the live model misses |
| 11 | — | service `reset_learning` needs `confirm: true`; scopes `stage1` / `stage2` / `all`; removing the hub keeps the store and the export | months of evidence are not lost by an accidental removal |
| 12 | — | station time: sensor state (timestamp); fallbacks: a time attribute, then `age_minutes` rounded to 10 min; states older than 60 min ignored | robust to Fusion attribute changes; reported in the state sensor |
| 13 | — | production from recorder long-term statistics (hourly change, converted to kWh); 400 days backfilled at first run, then the last 10 days nightly | restarts never lose production data |
| 15 | station values only from the latest record | 0.1.34.1: also every record of Fusion's `recent_values` (>= 0.3.5, last 6 h; merged by time, replaced only by an equally or more complete record; window 7 h, at most 100 entries); also read in the startup backfill | live system 13 Oct: about every second record never reached the state, so no hour reached 5 of 6 |
| 14 | — | Home Assistant platform: `ConfigEntry.runtime_data` owns coordinator / hub; `PARALLEL_UPDATES` (sensors 0, switches 1); owner actions serialised with the runs; every computation in the executor on a consistent copy of the data; recorder only through its own executor | explicit lifecycle ownership, deliberate concurrency (owner request, 13 Oct 2026) |
