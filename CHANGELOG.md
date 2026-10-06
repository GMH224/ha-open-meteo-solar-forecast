# Changelog — GMH224 fork

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
