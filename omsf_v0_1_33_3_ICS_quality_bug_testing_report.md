# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.3 — ICS Quality Bug & Testing Report

**Release:** v0.1.33.3 (hybrid weather source, release-process fix)
**Date:** 6 October 2026
**Suite:** 203 tests passing (+144 subtests), up from 175 in 0.1.33.2
**Static analysis:** `pyflakes custom_components tests`: clean
**Mutation testing:** 50/50 caught (37 carried from 0.1.33.2 and re-run, 13 new; main run 46/50 → one escape fixed by a stronger test, three patterns re-pointed)
**Standard applied:** *if I break it, does a test fail?*

---

## 1. Scope of testing

Three questions:

1. **Is hybrid mode correct?** Day selection, join, timezone handling, the
   Open-Meteo cadence and cache, failure isolation, provenance, persistence,
   configuration.
2. **Are Open-Meteo mode and local mode unchanged?** All 0.1.33.2 tests run
   unchanged (175/175), plus explicit tests that hybrid code is not reached in
   the other modes.
3. **Is the release-process fix in place?** HACS configuration and workflow.

Environment as in 0.1.33.2: Python 3.13, Home Assistant 2026.2.3 via
pytest-homeassistant-custom-component 0.13.316 (sockets disabled), library
0.1.32, pvlib 0.16.1. In addition, 0.1.33.2 has now run live on Home Assistant
2026.9.4 / Python 3.14.6 (release audit §2).

---

## 2. Test inventory

| File | Tests | New in 0.1.33.3 | What it proves |
|---|---|---|---|
| `test_hybrid.py` | 12 | all | Purity; local days from local, all others from Open-Meteo; no day mixes sources (every series, distinct values per source); a complete local day wins over Open-Meteo; partial local data dropped; no Open-Meteo → local days only; incomplete Open-Meteo day refused; 23-hour DST day accepted; Open-Meteo in another UTC offset regrouped into local days with correct totals; daily totals = Σ selected hours; sorted output; empty case |
| `test_hybrid_integration.py` | 16 | all | Real HA: the owner's 45-hour case (today + days 2–7 Open-Meteo, tomorrow local, sensors and `source` attributes); 120-hour case; midnight seam (zero power both sides, no leakage); 30-minute cadence; Open-Meteo down (local days survive, others unknown); 3-hour cache then unavailable; stale local data in hybrid (no silent switch; explicit fallback = full Open-Meteo); retain and recover; day sources across restart; 0.1.33.2 retained data read on upgrade; local mode marks days local and never calls Open-Meteo; Open-Meteo mode has no `source` attribute; config flow; diagnostics; release process |
| `test_local_integration.py` | 37 | — | unchanged, all pass |
| `test_local_source.py` | 85 | — | unchanged, all pass |
| `test_local_physics_validation.py` | 45 | — | unchanged, all pass |
| `test_translations_local.py` | 5 | (coverage extends automatically: selector options = `WEATHER_SOURCES`, states = `ACTIVE_SOURCES`) | hybrid strings in strings/en/de |
| `test_config_flow.py` | 3 (+144 subtests) | — | unchanged |

The Open-Meteo estimate in the integration tests is a stub (8 full days,
500 Wh per daylight hour), distinct from the local physics values, so every
assertion can tell which source a day came from.

---

## 3. Defects found

| ID | Severity | Found by |
|---|---|---|
| P-09 | Medium (process) | live: the 0.1.33.2 HACS download failed ("Could not download"); root cause traced to the inherited `zip_release` and a never-run workflow |
| LV-1…LV-4 | external / configuration | live diagnostics review (release audit §2) |
| test expectation (Open-Meteo-down status) | — (own) | first run of the new tests |
| unused imports | — (own) | pyflakes |
| timezone test checked only an interior day | — (own, test quality) | mutation M40 escaped |

No defect in the 0.1.33.2 code paths was found by the live run.

---

## 4. Test-quality notes

- **Distinct values per source.** The first idea was to check day provenance
  through `day_sources` only. That would pass even if the join copied the
  wrong data. The tests instead give the two sources different values and
  check the actual series, per day, for watts, hourly and 15-minute energy.
- **Timezone case.** A join that grouped days in the source's own timezone
  would pass every test where both use CEST. A dedicated test uses a UTC
  Open-Meteo estimate (mutation M40).
- **Failure isolation tested both ways.** Open-Meteo failing must not remove
  the local days (M44), and local failing must not silently become Open-Meteo
  (M14, M23).

---

## 5. Mutation testing

Runner and method as in 0.1.33.2 (`tests/mutation/run_mutations.py`; each
mutation applied to a temporary copy, full suite with `-x`; NOT APPLIED is
reported, never skipped). All 37 mutations of 0.1.33.2 were re-run against
0.1.33.3, plus 13 new ones (M38–M50). Raw results:
`tests/mutation/mutation_results_0.1.33.3.json`.

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

## 6. What was not tested

- **Hybrid mode live.** Open-Meteo is a stub in the tests. First acceptance
  step on the owner's system: diagnostics `source.day_sources` and
  `hybrid_open_meteo.open_meteo_days`.
- **Real model horizons on Open-Meteo** (seamless MeteoSwiss, ICON-CH2,
  best_match) — documentation-based (L-9).
- **HACS install from the tag** — configuration and test, not a real HACS
  download of this release.
- Carried from 0.1.33.2: one HA version in the suite; no accuracy comparison
  against measured production; DST transition inside the horizon; frontend
  rendering.

---

## 7. Reproducing this report

```bash
pip install -r requirements-test.txt
python -m pytest -q                       # 203 passed, 144 subtests passed
python -m pyflakes custom_components tests
python tests/mutation/run_mutations.py    # ~40 min
```
