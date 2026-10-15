# Changelog — GMH224 fork

## Unreleased — public-repository clean-up (2026-10-15, no version change)

- Private installation data removed from the public files: test locations
  near the owner's home replaced by a neutral public location (Zurich city
  centre), the reference station and town names and its distance replaced by
  generic wording, the inverter brand removed from examples and audits.
  No code behaviour changed.
- New guard `tests/test_public_release.py`: no e-mail addresses; with the
  private `confidential.md` available, none of its listed strings may appear
  in any file. `confidential.md` is git-ignored and `export-ignore`.

## 0.1.34.2 — 2026-10-14 — total cloud, cell temperature, two-year memory

Design confirmed by the owner on 14 Oct 2026 (spec §4, §6, §8, §17 rows
16–20). Release audit: `omsf_v0.1.34.2_release_audit.md`.

### Changed (learning hub)
- **Total cloud** is a sixth predictor of the weather correction (scale
  30 %): some models behind Fusion give only total cloud.
- **Calibration per inverter by sky type × cell temperature** (9 cells
  instead of 3). Cell temperature is estimated as air temperature plus
  0.0342 K·m²/W × sunlight (the panels heat up in the sun; UV is not the
  driver and the thermal lag stays inside the hour).
- **Shaded hours left out of the calibration:** the nightly model re-run also
  computes the model without the shading geometry; hours in which the
  geometry removes more than 10 % are not used.
- **Two-year memory:** retention 760 days (was 400), evidence up to 740 days;
  weight half-life 90 days (was 8 weeks), never below 10 %; the same season
  a year earlier (±45 days) keeps at least 70 % (was ±30 days, 50 %). The
  calibration uses all stored hours with the same weights (was the last
  8 weeks). Store limit 16 MB (was 8 MB).
- `Calibration <group>` sensor state: the overcast/cool cell; the efficiency
  suggestion comes from the clear/warm cell.
- Export: production file gains `<group>_model_unshaded_kwh`.

### Fixed
- The store-size trim was capped at 400 days; now tied to the retention.

### Upgrade note
- The learning store is migrated once (schema 1 → 2, event `migrated`).
  Station hours, production, models, day decisions, the switches and the
  event log are kept. The forecast archive (rows without total cloud), the
  06:00 records and the old calibration are dropped, so the 28-day skill
  check and the evidence count restart from the upgrade day.

## 0.1.34.1 — 2026-10-13 — every station record (Fusion 0.3.5)

Release audit: `omsf_v0.1.34.1_release_audit.md`.

### Fixed
- **No station hour was ever complete on the live system** (13 Oct: 10 of
  19 ten-minute records reached the sensor state; MeteoSwiss publishes with
  a varying delay, so polling the latest value skips records). The hub now
  reads Fusion's new `recent_values` attribute (>= 0.3.5: every record of
  the last 6 hours) on every update, in the hourly run and in the startup
  backfill from the recorder history. The strict 5-of-6 rule is unchanged.
  A record is replaced only by one at least as complete.
- The state sensor's `since` was empty after a fresh start.
- The startup backfill's outcome was overwritten by the hourly report; it is
  now kept (`last_run` attribute `startup`, diagnostics `last_startup`).

### Unchanged
- With Fusion < 0.3.5 (no `recent_values`) everything behaves as in 0.1.34.

## 0.1.34 — 2026-10-13 — learning hub (self-learning weather correction)

Design record: `OMSF_v0_1_34_Learning_Spec.md` (confirmed by the owner on
12 Oct 2026). Release audit: `omsf_v0.1.34_release_audit.md`.

### Added
- **Learning hub** (new entry type, at most one): learns from the MeteoSwiss
  reference station (SwissWeather Fusion >= 0.3.4) and the inverters'
  energy counters how the local weather forecast errs, and corrects it.
  Stage 1: analog ensemble on six predictors (forecast clearness, sun
  elevation, low cloud, mid/high cloud, temperature - dew point, lead time),
  shrunk to neutral on thin evidence. Stage 2: per-inverter calibration by
  sky type from the model re-run on measured light.
- **Owner control and safety:** starts neutral; acts only when the nightly
  skill check (28 days, 06:00 forecast, >= 5 % better, energy not worse)
  passes **and** the switch *Apply learning* is on; falls back by itself;
  every fault leaves the forecast raw; corrections bounded.
- **Transparency:** 7 hub sensors + 4 per group, 2 switches, per-hour
  explanation (similar past hours), raw and corrected totals on every
  member's `forecast_source`, hub diagnostics with the event log, nightly
  CSV/JSON export to `/config/open_meteo_solar_forecast/export/`,
  services `reset_learning` (with confirmation) and `export_learning`.
- `tools/learning_replay.py`: independent re-implementation that reproduces
  every stored correction and the skill check from the export.
- `examples/dashboard/learning.yaml`: dashboard with core cards only.

### Changed
- **Home Assistant platform (owner request, 13 Oct 2026):** runtime objects
  owned by `ConfigEntry.runtime_data` (no `hass.data`), typed entry aliases
  and data models (`runtime.py`), coordinator bound to its entry,
  `PARALLEL_UPDATES` on both platforms, learning computations in the
  executor on consistent copies, owner actions serialised with the runs.
  mypy: new modules clean under strict settings; the 38 findings in older
  modules are unchanged from 0.1.33.5 (backlog).
- **South geometry example refitted for 40° roofs** (owner, 12 Oct 2026);
  the 45° version is kept as `examples/shading/south_geometry_45deg.yaml`.
- The setup dialog offers a menu (PV array / learning hub) once a PV entry
  exists and no hub does.
- Recorder: the hub's per-hour explanation list is not recorded.
- SRF comment updated (Fusion 0.3.4 documents the hour averaging).

### Unchanged
- PV entries that are not in an inverter group, and every Open-Meteo-mode
  entry, behave exactly as in 0.1.33.5. While the hub is not in state
  `applied`, member forecasts are also unchanged (shadow mode).

## 0.1.33.5 — 2026-10-11 — partial shading (geometry and table)

### Added
- **Shading file per array** (optional, setup and options dialog): either a
  **geometry file** (`.yaml`: module field incl. bypass diodes, cell type and
  strings, plus boxes, gable-roof buildings, planar polygons, cylinders and
  cones with optional monthly transparency) that is ray-traced into a table,
  or a **shading table** (`azimuth elevation factor`, optionally per month).
- Applied every 15 minutes to the **direct** light on the panel plane only,
  identically for Open-Meteo, local and hybrid data. Electrical models:
  bypass-diode/string (default) or area.
- Faulty file: the array runs unshaded, a repair issue explains why, the
  dialog refuses the file with the same message; `forecast_source` attribute
  `shading`; diagnostics block `shading` (incl. removed share per refresh).
- `examples/shading/`: reference example with every obstacle type, the
  fitted geometry of the owner's South array and its table.
  `tools/shading_table.py`: geometry → table / file check without HA.
- Tests: 126 new (geometry against hand-calculable shadow edges, electrical
  model hand-worked, table format, application against pvlib, HA
  integration in all three modes, flows, issues, upgrade path, examples);
  mutations M74–M108.

### Unchanged
- Arrays without a shading file: the unmodified library class is used
  (Open-Meteo parity); the horizon file and its partial-shading switch work
  as before.

## 0.1.33.4 — 2026-10-06 — external audit remediation

Remediates the external ICS/OT audit of 0.1.33.3 (20 findings; triage in
`omsf_v0.1.33.3_external_audit_triage.md`, account in
`omsf_v0.1.33.4_release_audit.md`). With the owner's consent, the
"Open-Meteo mode unchanged" rule is relaxed for these safety fixes.

### Fixed
- **Hybrid, Open-Meteo down → "power now" 0 W in daylight (V-1, found during
  verification) / empty forecast reported as success (OMSF-004).** Partial
  local days keep their data for intraday values; an empty forecast is a
  failure, never a success.
- **Unbounded local input (OMSF-007/008):** irradiance and temperature lists
  limited to 500 entries and to [now − 48 h, now + 10 days].
- **Hybrid completeness (OMSF-011):** an Open-Meteo day needs all 24 hours.
- **Secrets in error text (OMSF-018):** URL query strings and key-like
  parameters removed, length ≤ 200, before any attribute or diagnostics.
- **Service `update_array_location` (OMSF-001/002):** registered once;
  validated coordinates; with more than one entry, `config_entry_id` is
  required (previously the last-loaded entry was changed silently).
- **Retained forecast age (OMSF-003):** served for at most 6 hours, in every
  mode; then the forecast sensors are unavailable and `forecast_source` reads
  `stale`.
- **Horizon file (OMSF-009):** size/row limits, finite values, elevation
  range, controlled message for every I/O problem. Endpoint tolerance
  unchanged for compatibility.
- **Malformed retained store (OMSF-012):** shape and value validation; a
  malformed store is discarded instead of blocking setup.
- **History store (OMSF-013):** failed read retried up to 3 times.
- **Recorder (OMSF-019):** `wh_period_15m` excluded like the other series.
- **Defensive parsing (OMSF-015/016).**
- **Test harness (T-1):** the fake weather service returned the wrong keys,
  so integration tests since 0.1.33.2 ran on the temperature fallback.

### Changed
- Snow depth: last valid value held for 24 h before "unavailable" is treated
  as no snow (OMSF-005).
- `forecast_source` attribute `data_quality` (temperature: forecast /
  current-value fallback; snow depth: sensor / held / assumed 0) (OMSF-006).

### Declined (documented)
- OMSF-017 (custom base URL is a supported feature), OMSF-020 (several
  entries are intended). OMSF-010 (DST) disputed: effect confined to night
  hours.

## 0.1.33.3 — 2026-10-06

### Added
- **Hybrid weather source** (`hybrid`): every local day the local data covers
  completely comes from the local computation, every other day from
  Open-Meteo, joined at local midnight. Open-Meteo is queried every 30 minutes
  (cached between the 10-minute local refreshes); if it fails, a cached
  estimate is used for up to 3 hours, then those days become unknown while the
  local days continue.
- `source` attribute (`local` / `open_meteo`) on the day sensors in local and
  hybrid mode; `forecast_source` state `hybrid`, attribute `hybrid_open_meteo`;
  diagnostics `day_sources`.
- Tests for hybrid mode (28 new), 13 new mutations.

### Changed
- **Release process:** `hacs.json` no longer uses `zip_release` / `filename`;
  HACS installs directly from the tag, no zip upload needed. The inherited
  `release.yml` workflow is removed.
- Translations (EN/DE) for the hybrid option and state.

### Unchanged
- Open-Meteo mode and local mode behave as in 0.1.33.2 (local mode adds only
  the `source` attribute).

Full account: `omsf_v0.1.33.3_release_audit.md`.

## 0.1.33.2 — 2026-10-06

### Added
- **Local weather source.** New setup/options choice *Weather data source*:
  `Open-Meteo API` (default, unchanged) or `Local (Home Assistant entities)`.
  Local mode computes the forecast from SwissWeather Fusion ≥ 0.3.3
  (hourly GHI/DNI/DHI series + hourly temperature) without any internet
  access, refreshing every 10 minutes.
- Auto-discovery of Fusion's irradiance and snow-depth sensors from the
  selected weather entity.
- Optional, explicit fallback to Open-Meteo when local data is unusable
  (default off: the last good forecast is kept).
- Diagnostic sensor `sensor.<name>_forecast_source`
  (`open_meteo` / `local` / `open_meteo_fallback` / `retained`) with
  `last_error` and `last_successful_update`.
- Diagnostics: `source` block with data-quality counters per refresh.
- German translations for all new strings.
- Test suite on a real Home Assistant (175 tests, 144 subtests), pvlib
  validation, mutation record (37/37 caught); ICS documentation set.

### Changed
- In local mode, day-based energy sensors report *unknown* for days the local
  data does not fully cover (instead of 0 Wh or an undercount).
- `hacs.json` minimum Home Assistant raised from 2022.11 to 2024.12.0 to match
  what the code already required (P-03).
- `manifest.json`: version 0.1.33.2, documentation/issue tracker point to the
  fork, `after_dependencies: weather`.

### Fixed (test infrastructure)
- `tests/test_config_flow.py` could not be imported on Home Assistant 2026.2
  (`to_field_list` missing) (P-01).
- The pre-existing unittest-based tests left no event loop, breaking any
  Home Assistant test run after them in the same session (P-02).

### Unchanged
- Open-Meteo mode: library arguments, 30-minute interval, retained-forecast
  fingerprint. No migration.

Full account: `omsf_v0.1.33.2_release_audit.md`.

## 0.1.33.1
Baseline fork of rany2/ha-open-meteo-solar-forecast 0.1.33.
