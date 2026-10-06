# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.3 — Release Audit (ICS)

**Release type:** feature (hybrid weather source) plus release-process fix.
Baseline: v0.1.33.2 (same day). Library `open_meteo_solar_forecast==0.1.32`,
unchanged.
**Governing invariants:** (1) *Open-Meteo mode behaves exactly as in
0.1.33.1*, carried forward; (2) *local mode behaves exactly as in 0.1.33.2*,
except for one added attribute. Every change was checked against both.
**Verification:** 203 tests passing (+144 subtests) on Home Assistant
2026.2.3, pyflakes clean, mutation record §7. First live run of 0.1.33.2 on
the owner's installation recorded in §2.

---

## 1. What was added or changed

| Item | Detail |
|---|---|
| Hybrid source | Third option `hybrid`: local for every day the local data covers completely, Open-Meteo for every other day, joined at local midnight (`hybrid.py`, pure) |
| Open-Meteo part | Uses the entry's own Open-Meteo settings; refreshed at most every 30 min; failed refresh → cache ≤ 3 h → otherwise those days unknown; never fails the refresh |
| Provenance | `source` attribute on day sensors (local/hybrid modes); `forecast_source` state `hybrid` and attribute `hybrid_open_meteo`; diagnostics `day_sources` |
| Persistence | `day_sources` stored with the retained forecast; 0.1.33.2's `local_complete_dates` still read on upgrade |
| Release process | `hacs.json` without `zip_release`/`filename`; `release.yml` removed |
| Translations | EN/DE for the hybrid option, state and updated source description |
| Tests | 28 new (12 pure, 16 on Home Assistant), 13 new mutations |

**Deliberately not done:**

- **No hour-level splice.** Owner decision: whole days, seam at midnight. The
  local hours of a partly covered day are therefore not used (L-10).
- **No automatic model change.** In hybrid mode the Open-Meteo days use the
  model the owner configured; a MeteoSwiss-only model cannot fill days 6–7.
  Documented (L-9) and reported to the owner, not overridden silently.
- **No change to local or Open-Meteo mode behaviour**, including the "unknown"
  days in local mode.

---

## 2. Live verification of 0.1.33.2 (owner's installation, 6 Oct 2026)

Source: three diagnostics exports (entries East / South / West), Home
Assistant **2026.9.4, Python 3.14.6**, SwissWeather Fusion 0.3.3.

| Check | Result |
|---|---|
| Source on all three entries | `local`, no error, fallback off, interval 600 s |
| Irradiance series | 45 entries received, 44 averages / 45 instants accepted, **0 rejected** |
| Temperature series | 160 received, 160 accepted |
| DNI consistency (published DNI vs (GHI−DHI)/cos Z) | **3.7 W/m²** mean absolute deviation — Fusion's triple and this integration's hour alignment agree |
| Clearness clamps / temperature fallbacks | 0 / 0 |
| Plausibility, next day (clear October day) | South 6 kWp 45°: 4.8 kWh/kWp; East 7 kWp: 3.1; West 4 kWp: 2.0 |
| Horizon file effect | visible as steps (East at ~08:00, West at ~17:45) — expected |

This closes the 0.1.33.2 gap "no run on a live Home Assistant with the real
Fusion" for the input contract. It also verifies a Home Assistant / Python
version newer than the test environment.

**Findings from the live run:**

- **LV-1 (input, external) — Fusion delivered ~45 h instead of ~120 h**,
  `current_hour_sources = 2`. ICON-CH2 (120 h, per MeteoSwiss and Open-Meteo
  documentation) was not contributing; ICON-CH1 (33 h) and ICON-D2 (~48 h)
  were. Either the first CH2 run with radiation had not yet arrived (Fusion
  v0.3.3 audit §3: ≤ 6 h) or CH2 rejects a radiation variable (Fusion's
  per-model optional-variable fallback). To be checked on the Fusion side;
  not a defect of this integration.
- **LV-2 (presentation) — "unknown" drawn as 0.0.** The owner's dashboard
  card shows unknown day sensors as 0.0 kWh, which reads as a forecast of
  zero. The 0.1.33.2 behaviour is correct, but its effect in practice was a
  misleading chart. **This motivated hybrid mode.**
- **LV-3 (configuration, owner) — shared inverter modelled per entry.** West
  and South share one 10 kW inverter but are separate entries, each clamped
  to 10 kW on its own. Not binding in October (combined peak ≈ 6.4 kW); can
  be in summer. Recommendation: one entry with two arrays and a shared
  10 kW capacity.
- **LV-4 (configuration, owner) — model choice.** East uses
  `meteoswiss_icon_ch2`, South/West `meteoswiss_icon_seamless`. Irrelevant in
  local mode; in hybrid mode it limits the Open-Meteo days (L-9).

---

## 3. Design decisions

**Join by whole local days (owner decision).** PV output is zero at local
midnight, so joining there creates no step in the power curve, and every
day's total, peak time and curve come from one source. The alternative —
hour-level splice — would put the seam in daylight and mix sources within
a day, for a small gain in local data on the partly covered day.

**Day selection = completeness, not position.** A day is local if the local
data covers all its daylight hours, otherwise Open-Meteo if Open-Meteo has
≥ 23 hours. This handles every case with one rule: today after installation
(no history → Open-Meteo), the partly covered last local day (→ Open-Meteo),
gaps in the local series (→ Open-Meteo), and an Open-Meteo model that ends
early (→ unknown).

**Daily totals recomputed from the selected hours,** in the local estimate's
timezone. Open-Meteo's `timezone=auto` can differ from Home Assistant's; the
library's own daily totals would then be grouped on different day
boundaries. Tested with a UTC Open-Meteo estimate against a CEST local one.

**Open-Meteo cadence 30 min, cache 3 h.** Same request load as Open-Meteo
mode. Three hours of tolerance covers typical API outages; beyond that, a
stale far-future forecast is worse than an honest unknown.

**The Open-Meteo part never fails the refresh.** In hybrid mode the local
days are the primary product; losing the extension must not lose them
(F13). Conversely, a *local* failure is handled before the join, exactly as
in local mode — hybrid mode does not quietly turn into Open-Meteo mode
(`test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo`).

---

## 4. Invariant analysis

| Invariant | Risk | Mitigation | Test |
|---|---|---|---|
| Open-Meteo mode unchanged | hybrid code reached; attributes added | all new paths behind `uses_local` / `SOURCE_HYBRID`; `source` attribute only when `day_sources` is set | all 0.1.33.2 parity tests; `test_open_meteo_mode_day_sensors_have_no_source_attribute`; M49 |
| Local mode unchanged | hybrid join applied; Open-Meteo called | hybrid branch only for `weather_source == "hybrid"` | `test_local_mode_marks_its_days_local_and_never_calls_open_meteo`; all 0.1.33.2 local tests; M45 |
| Local mode offline | hybrid code calls Open-Meteo | network guard in every local test | as above |
| Upgrade 0.1.33.2 → 0.1.33.3 | retained coverage lost | legacy key read | `test_a_retained_forecast_from_0_1_33_2_is_read_as_local_days`; M48 |

The one deliberate change in local mode: day sensors gain `source: local`.

---

## 5. Defects

### 5.1 Fixed

**P-09 (Medium, process; from 0.1.33.2 backlog item 7) — release needed a
manually attached zip.** `hacs.json` (inherited) required a release asset
built by `release.yml`, a workflow GitHub never ran on the fork. The 0.1.33.2
release failed in HACS with "Could not download" until the zip was attached
by hand. **Fix:** `zip_release`/`filename` removed, `release.yml` removed;
test and mutation M50 guard it.

### 5.2 Found in this pass's own work (before release)

- **Test expectation incomplete.** The first version of the Open-Meteo-down
  test compared the status dictionary exactly and missed the
  `open_meteo_days` key; the implementation was right, the test was
  rewritten to check each field.
- **Unused imports** in the new integration test file (pyflakes), removed.
- **Timezone test too weak (mutation M40 escaped).** The test for an
  Open-Meteo estimate in another UTC offset checked one interior day only,
  where grouping by the source's own offset cancels out. Grouping in the
  wrong timezone would have lost two hours on the first Open-Meteo day after
  the local days. The test now checks all 24 hours of every day.
- **Document structure.** The hybrid section was first inserted before §5.8
  of the architecture document; moved to §5.9.

The mutation results in §7 record whether any rule was untested.

---

## 6. Verification record

| Item | Result |
|---|---|
| Test suite | 203 passed, 144 subtests passed |
| New tests | `test_hybrid.py` 12, `test_hybrid_integration.py` 16 |
| Environment | Python 3.13, Home Assistant 2026.2.3 (pytest-homeassistant-custom-component 0.13.316), library 0.1.32, pvlib 0.16.1 |
| Live | 0.1.33.2 on Home Assistant 2026.9.4 / Python 3.14.6 (§2) |
| Static analysis | pyflakes clean |
| Mutation testing | §7 |

---

## 7. Mutation record

| # | Mutation | First failing test |
|---|---|---|
| M01 | average labelled at the quarter START instead of its end | `test_local_physics_validation.py::test_daily_energy_through_the_library_matches_a_one_minute_reference[30-90-0.015]` |
| M02 | first label gets an average (covers the hour before the data) | `test_local_source.py::test_the_first_label_carries_no_average_because_it_would_cover_the_previous_hour` |
| M03 | clearness index not clamped | `test_local_source.py::test_an_implausible_hour_average_is_clamped_and_counted` |
| M04 | D-01 regression: hour clearness on a finer grid than the quarters | `test_local_source.py::test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M05 | D-02 regression: low-sun beam not reassigned to diffuse | `test_local_source.py::test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi` |
| M06 | anisotropy index not clamped to 1 | `test_local_physics_validation.py::test_anisotropy_is_clamped_where_the_input_implies_dni_above_e0` |
| M07 | azimuth convention: Open-Meteo value used as compass bearing | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M08 | ground-reflected term dropped | `test_local_physics_validation.py::test_hay_davies_transposition_matches_pvlib[7-30-0]` |
| M09 | published instants ignored (hour-average anchor only) | `test_local_source.py::test_instant_values_at_hour_starts_reproduce_the_published_instants` |
| M10 | diffuse-exceeds-global check removed | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change2]` |
| M11 | incomplete triple accepted | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change4]` |
| M12 | booleans accepted as numbers | `test_local_source.py::test_an_invalid_average_triple_is_dropped_but_the_instant_survives[change8]` |
| M13 | non-hour-aligned period_start accepted | `test_local_source.py::test_entries_with_bad_timestamps_are_rejected_and_reported[change2-not on a full hour]` |
| M14 | stale-series check removed | `test_hybrid_integration.py::test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo` |
| M15 | current-hour requirement removed | `test_local_source.py::test_a_series_without_the_current_hour_average_is_refused` |
| M16 | history resurrects future hours from cache | `test_local_source.py::test_history_never_resurrects_a_future_hour_the_source_dropped` |
| M17 | history not pruned | `test_local_source.py::test_history_is_pruned_before_keep_from` |
| M18 | Fahrenheit not converted | `test_local_source.py::test_temperature_forecast_converts_fahrenheit_and_kelvin` |
| M19 | temperature gaps bridged without limit | `test_local_source.py::test_temperature_gaps_longer_than_three_hours_are_not_bridged` |
| M20 | snow depth not applied | `test_local_integration.py::test_snow_depth_reduces_output_only_when_snow_derating_is_enabled` |
| M21 | polar day/night can yield sunset == sunrise | `test_local_source.py::test_polar_night_and_polar_day_never_produce_a_zero_length_day` |
| M22 | fallback to Open-Meteo on by default | `test_local_integration.py::test_fallback_is_off_when_the_option_is_absent` |
| M23 | fallback taken although disabled | `test_hybrid_integration.py::test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo` |
| M24 | local refresh interval 30 min | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M25 | Open-Meteo refresh interval changed | `test_local_integration.py::test_open_meteo_mode_is_unchanged_and_never_reads_local_entities` |
| M26 | retained-source state not reported *(see note)* | `test_hybrid_integration.py::test_hybrid_retains_and_recovers_like_local_mode` |
| M27 | history never persisted | `test_local_integration.py::test_history_survives_a_restart` |
| M28 | Fusion companion suffix wrong | `test_hybrid_integration.py::test_config_flow_hybrid_asks_for_the_local_entities` |
| M29 | missing temperature accepted (no current temperature either) | `test_local_integration.py::test_no_temperature_at_all_is_refused` |
| M30 | history merge skipped (only the fresh series is used) | `test_local_integration.py::test_energy_today_keeps_elapsed_hours_after_the_source_moves_on` |
| M31 | D-05 regression: day sensors not masked in local mode | `test_hybrid_integration.py::test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown` |
| M32 | night hours treated as daylight in day coverage | `test_local_integration.py::test_today_becomes_known_once_history_covers_the_morning` |
| M33 | fallback keeps the local coverage mask on Open-Meteo data *(see note)* | `test_local_integration.py::test_fallback_clears_the_local_day_mask` |
| M34 | day coverage not persisted with the retained forecast | `test_local_integration.py::test_day_coverage_survives_a_restart_with_the_retained_forecast` |
| M35 | D-06 regression: weather service call unbounded | `test_local_integration.py::test_a_hung_weather_service_degrades_within_the_bound` |
| M36 | D-07 regression: missing stored coverage masks every day *(see note)* | `test_local_integration.py::test_a_retained_fallback_forecast_is_not_masked_after_restart` |
| M37 | parity: Open-Meteo failure message changed | `test_local_integration.py::test_open_meteo_mode_failure_text_and_logging_are_unchanged` |
| M38 | hybrid: Open-Meteo overrides a complete local day | `test_hybrid.py::test_complete_local_days_come_from_local_and_all_other_days_from_open_meteo` |
| M39 | hybrid: incomplete Open-Meteo days accepted | `test_hybrid.py::test_an_incomplete_open_meteo_day_is_not_used` |
| M40 | hybrid: days taken in the source's own timezone *(see note)* | `test_hybrid.py::test_open_meteo_in_another_offset_is_regrouped_into_local_days` |
| M41 | hybrid: Open-Meteo fetched every local cycle | `test_hybrid_integration.py::test_open_meteo_is_refreshed_every_30_minutes_not_every_cycle` |
| M42 | hybrid: no cache after a failed refresh | `test_hybrid_integration.py::test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours` |
| M43 | hybrid: cache used without age limit | `test_hybrid_integration.py::test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours` |
| M44 | hybrid: Open-Meteo failure fails the whole refresh | `test_hybrid_integration.py::test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown` |
| M45 | hybrid behaves like local (no Open-Meteo days) | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M46 | hybrid not treated as a local-based source | `test_hybrid_integration.py::test_short_local_horizon_is_completed_by_open_meteo_day_by_day` |
| M47 | day sources not persisted | `test_hybrid_integration.py::test_day_sources_survive_a_restart` |
| M48 | 0.1.33.2 retained coverage not read after upgrade | `test_hybrid_integration.py::test_a_retained_forecast_from_0_1_33_2_is_read_as_local_days` |
| M49 | source attribute added in Open-Meteo mode | `test_hybrid_integration.py::test_open_meteo_mode_day_sensors_have_no_source_attribute` |
| M50 | release process: zip_release restored | `test_hybrid_integration.py::test_hacs_installs_from_the_tag_without_a_release_zip` |

Notes: M26, M33 and M36 were NOT APPLIED in the main run because the hybrid
refactor changed the code they target (`uses_local`, `_set_day_sources`);
their patterns were updated and each was re-run alone: caught. **M40 escaped**
the main run: the timezone test checked only an interior day, where grouping
in the source's own UTC offset cancels out; it now checks every day's 24 hours
including the day right after the local day, and M40 is caught.

Run history 0.1.33.3: main run 46/50 caught (3 not applied, 1 escaped) →
after pattern updates and the strengthened test: **50/50**.

---

## 8. Verification gaps

- **Hybrid mode not yet run live.** The Open-Meteo part is exercised with a
  stub estimate; its real coverage per model (days 6–7) is documentation-
  based. First acceptance step: diagnostics `source.day_sources` and
  `hybrid_open_meteo.open_meteo_days` on the owner's entries.
- **Seamless MeteoSwiss model horizon** not verified (L-9 states the
  consequence either way).
- **HACS install from tag without zip** verified by configuration and test,
  not yet by a real HACS download of this release.
- Carried forward from 0.1.33.2: one HA version in tests, no accuracy
  comparison against measured production.

---

## 9. Backlog after v0.1.33.3

1. LV-1: confirm ICON-CH2 radiation arrives in Fusion (`received` ≈ 120,
   `current_hour_sources` = 3); otherwise investigate Fusion's CH2 request.
2. LV-3: model West + South as one entry with a shared 10 kW inverter.
3. LV-4: `best_match` for hybrid entries that should reach days 6–7.
4. Re-evaluate tuning factors (West efficiency 0.8 and morning damping 1.0,
   East evening damping 0.9) against Huawei production data under local
   physics.
5. Carried: bias correction against inverter data; P-04 … P-08; snow albedo;
   SRF disagreement flag; capture a live Fusion attribute as a fixture (the
   §2 export now provides the shape).
6. External audit of 0.1.33.3 (owner plan), after a short live hybrid run.
