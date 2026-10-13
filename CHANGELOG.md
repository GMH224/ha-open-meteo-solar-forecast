# Changelog — GMH224 fork

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
