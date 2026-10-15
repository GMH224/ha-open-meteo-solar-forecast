# Developer notes — GMH224 fork of ha-open-meteo-solar-forecast

Applies from v0.1.33.2 (updated for 0.1.34 / 0.1.34.1). Architecture and rationale:
[`OMSF_v0_1_33_2_Architecture_ICS.md`](OMSF_v0_1_33_2_Architecture_ICS.md).

## Module map

| Module | Role | Home Assistant imports |
|---|---|---|
| `__init__.py` | entry setup / unload / removal (also deletes the local history store); services; 0.1.34: two entry types (PV array, learning hub), each owning its runtime object in `runtime_data` | yes |
| `runtime.py` | typed entry aliases (`ArrayConfigEntry`, `HubConfigEntry`), TypedDict models, loaded-entry lookups (0.1.34) | yes (types only) |
| `learning.py` | **pure** learning engine: parsing, predictors, archive, analog ensemble, application, stage 2, quality control, gate, state, store (de)serialisation, export text (0.1.34) | **no** (enforced by test) |
| `learning_hub.py` | HA glue for learning: store, station listener, hourly/nightly runs, recorder statistics, coordinator interface, export files (0.1.34) | yes |
| `learning_entities.py`, `switch.py` | hub sensors and switches (0.1.34) | yes |
| `config_flow.py` | setup + options wizard; `local` step, validation, auto-discovery | yes |
| `coordinator.py` | refresh cycle; source selection; retained forecast; fallback policy; day coverage | yes |
| `local_provider.py` | **HA glue for the local source**: live entity reads, history store, `LocalOpenMeteoSolarForecast` adapter, Fusion companion discovery | yes |
| `local_source.py` | **pure engine**: validation, solar geometry, downscaling, transposition, payload synthesis, coverage | **no** (enforced by test) |
| `hybrid.py` | **pure**: day-level join of local and Open-Meteo estimates (0.1.33.3) | **no** (enforced by test) |
| `errors.py` | **pure**: sanitising error text before it reaches attributes/diagnostics (0.1.33.4) | **no** |
| `shading.py` | **pure**: shading table format, YAML geometry (validation, ray tracing, bypass-diode model), vectorised sun position, application to a response (0.1.33.5) | **no** (enforced by test) |
| `shading_provider.py` | HA glue for shading: bounded file read + cache, repair issues, `ShadingMixin`, `ShadedOpenMeteoSolarForecast` (0.1.33.5) | yes |
| `sensor.py` | forecast sensors + `forecast_source` diagnostic sensor; day masking | yes |
| `diagnostics.py` | export incl. `source` block | yes |
| `energy.py`, `recorder.py` | energy platform (0.1.34: via `runtime_data`, never the hub); recorder exclusions (0.1.34: + `hours`) | yes |

The upstream library `open_meteo_solar_forecast` is pinned to `==0.1.32`.
The adapter depends on two library internals: the `_request(uri, *, params)`
method and the fields it reads from the response (asserted by
`test_payload_has_exactly_the_fields_the_library_reads`). **Re-run the full
suite before changing the pin.**

## Rules for this codebase

1. **Open-Meteo mode must not change** unless that is the explicit purpose
   of a release. (0.1.33.4 relaxed this, with the owner's consent, for the
   safety fixes of the external audit; each deviation is listed in its
   release audit.) Every local-mode code path branches on
   `weather_source == "local"`; parity tests and mutation M25 guard this.
2. `local_source.py` stays pure (no HA, no I/O, no clock). Pass `now` in.
3. Untrusted input (entity states/attributes) is validated before use;
   rejection lists stay bounded.
4. A failure in local mode raises `LocalDataError`; it must never escape to
   Home Assistant as an unhandled exception.
5. Every new rule gets a test that fails when the rule is removed, and an
   entry in `tests/mutation/run_mutations.py`.
6. Every externally controlled collection has a size bound and every time
   series a time window (0.1.33.4). Mutation testing cannot find a *missing*
   rule; review each new input against this list.
7. Error text reaching an attribute or the diagnostics goes through
   `errors.sanitize_error`.
8. Shading (0.1.33.5) only ever reduces the **direct** part of the
   plane-of-array irradiance, and a table factor of 1 must leave a value
   bit-identical (`test_factor_one_leaves_the_payload_unchanged`, M108).
   Without any valid shading table the coordinator must keep the
   unmodified library class (M82).

9. Learning (0.1.34) never fails, blocks or delays a forecast beyond its own
   30 s bound: every learner fault leaves the forecast raw (M171–M173, M178).
   The corrected forecast is served **only** in hub state `applied`
   (gate passed and owner switch on; M170, M160, M174). Members not in a group and
   Open-Meteo entries are untouched (`test_entry_without_hub_or_group_*`).
10. No look-ahead: evidence is usable only after its day's quality decision,
    and a correction uses only evidence decided before its issue time
    (M124–M125). Stored corrections must stay reproducible from the export:
    run `tools/learning_replay.py` in the tests (`test_learning_replay.py`);
    any change that alters corrections bumps `learning.ALGORITHM_VERSION`.
11. Home Assistant ownership and concurrency (0.1.34): runtime objects live
    in `ConfigEntry.runtime_data` (never `hass.data`); cross-entry lookups
    go through `runtime.py` and return only loaded entries. Work in a worker
    thread receives `LearningData.view()`, never the live object; owner
    actions take the hub lock; the recorder is used only through
    `get_instance(hass).async_add_executor_job`. New and changed modules
    must pass `mypy` (`mypy.ini`) without new findings.
12. The learning store is versioned (`learning.STORE_SCHEMA_VERSION`).
    A change to the stored shape bumps it and adds a migration in
    `LearningData._from_json` that keeps every measurement it can
    (station hours, production, models, day decisions; 0.1.34.2: schema 1
    → 2, M211, M212, M217). Never discard measured data to simplify a
    migration: the owner relies on two years of memory for the seasons.
13. The repository is public. No private installation data in any file:
    no home or station location, no hardware brand/serials, no entry ids,
    no e-mail addresses. Tests use the neutral location in
    `tests/fusion_fixtures.py` (Zurich city centre). Private data live in
    `confidential.md` **outside** the repository (`../confidential.md`;
    git-ignored and `export-ignore` if copied in); `tests/test_public_release.py`
    fails if any string of its *Never publish* list appears in a file.

## Running the tests

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q            # full suite (~15 s)
.venv/bin/python -m pyflakes custom_components tests tools
.venv/bin/python -m mypy custom_components/open_meteo_solar_forecast   # 38 known findings in pre-0.1.34 modules, none new
.venv/bin/python tests/mutation/run_mutations.py --out mutation_results.json   # 190 mutations, hours on 2 CPUs; --from/--to/--only
```

`pytest-homeassistant-custom-component` brings a real Home Assistant and
disables network sockets. pvlib is a test-only reference; tests needing it
are skipped if it is absent (the test report states which).

Test files:

| File | Scope |
|---|---|
| `tests/test_local_source.py` | engine unit tests, rule by rule |
| `tests/test_local_physics_validation.py` | against pvlib: solar position, sunrise/sunset, Hay–Davies, end-to-end daily energy through the library |
| `tests/test_local_integration.py` | real HA: setup, failure modes, fallback, history, restart, snow, diagnostics, multi-array, config/options flow, Open-Meteo parity |
| `tests/test_translations_local.py` | translation coverage of every new UI string and error key |
| `tests/test_hybrid.py` | hybrid join rule, pure (0.1.33.3) |
| `tests/test_hybrid_integration.py` | real HA: hybrid cases, Open-Meteo cache/failure, provenance, parity, release process (0.1.33.3) |
| `tests/test_v0_1_33_4_audit_remediation.py` | regression tests for every remediated external-audit finding (0.1.33.4) |
| `tests/test_shading_geometry.py` | geometry: every obstacle type against hand-calculable shadow edges, electrical model hand-worked, validation messages, work budget (0.1.33.5) |
| `tests/test_shading_table_apply.py` | table format, interpolation, vectorised sun vs engine and pvlib, application against pvlib beam (0.1.33.5) |
| `tests/test_shading_integration.py` | real HA: Open-Meteo/local/hybrid with shading, parity without, repair issues, flows, cache, diagnostics, upgrade (0.1.33.5) |
| `tests/test_shading_examples.py` | shipped examples, South geometry vs fitted analysis, CLI tool, translations, purity (0.1.33.5) |
| `tests/test_learning_engine.py` | learning engine rule by rule against hand calculations; synthetic bias recovery; store validation (0.1.34) |
| `tests/test_learning_hub.py` | real HA: hub setup, shadow/applied/raw serving, fail-safe, station parsing, switches, services, export, flows, dashboard, scheduling, concurrency, ownership (0.1.34) |
| `tests/test_learning_recorder.py` | real HA with the in-memory recorder: production statistics, nightly run, station backfill (0.1.34) |
| `tests/test_learning_replay.py` | the independent replay tool reproduces every stored correction and the gate; detects tampering (0.1.34) |
| `tests/test_config_flow.py` | pre-existing wizard tests (with HA-version shim) |
| `tests/fusion_fixtures.py` | builders for the exact Fusion v0.3.3 attribute shape |
| `tests/mutation/run_mutations.py` | mutation record |

## Releasing

From 0.1.33.3 `hacs.json` has no `zip_release`: HACS installs the
`custom_components/open_meteo_solar_forecast/` folder straight from the tag.
A release is: bump `manifest.json` version, commit, create a GitHub release
with a new tag. No asset upload. Before releasing, run the suite with
`confidential.md` available so `test_public_release.py` actually scans. (0.1.33.2 still needed the manually attached
zip; the inherited `release.yml` that built it is removed. Guarded by
`test_hacs_installs_from_the_tag_without_a_release_zip`.)

## Honest gaps

See §8 of the release audit and §7 of the test report. In short: no run on
a live Home Assistant with the real Fusion integration; the Fusion attribute
shape is reproduced from Fusion's source code, not captured from a live
instance; Home Assistant verified at 2026.2.3 only.
