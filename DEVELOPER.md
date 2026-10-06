# Developer notes — GMH224 fork of ha-open-meteo-solar-forecast

Applies from v0.1.33.2. Architecture and rationale:
[`OMSF_v0_1_33_2_Architecture_ICS.md`](OMSF_v0_1_33_2_Architecture_ICS.md).

## Module map

| Module | Role | Home Assistant imports |
|---|---|---|
| `__init__.py` | entry setup / unload / removal (also deletes the local history store) | yes |
| `config_flow.py` | setup + options wizard; `local` step, validation, auto-discovery | yes |
| `coordinator.py` | refresh cycle; source selection; retained forecast; fallback policy; day coverage | yes |
| `local_provider.py` | **HA glue for the local source**: live entity reads, history store, `LocalOpenMeteoSolarForecast` adapter, Fusion companion discovery | yes |
| `local_source.py` | **pure engine**: validation, solar geometry, downscaling, transposition, payload synthesis, coverage | **no** (enforced by test) |
| `hybrid.py` | **pure**: day-level join of local and Open-Meteo estimates (0.1.33.3) | **no** (enforced by test) |
| `sensor.py` | forecast sensors + `forecast_source` diagnostic sensor; day masking | yes |
| `diagnostics.py` | export incl. `source` block | yes |
| `energy.py`, `recorder.py` | unchanged | yes |

The upstream library `open_meteo_solar_forecast` is pinned to `==0.1.32`.
The adapter depends on two library internals: the `_request(uri, *, params)`
method and the fields it reads from the response (asserted by
`test_payload_has_exactly_the_fields_the_library_reads`). **Re-run the full
suite before changing the pin.**

## Rules for this codebase

1. **Open-Meteo mode must not change** unless that is the explicit purpose
   of a release. Every local-mode code path branches on
   `weather_source == "local"`; parity tests and mutation M25 guard this.
2. `local_source.py` stays pure (no HA, no I/O, no clock). Pass `now` in.
3. Untrusted input (entity states/attributes) is validated before use;
   rejection lists stay bounded.
4. A failure in local mode raises `LocalDataError`; it must never escape to
   Home Assistant as an unhandled exception.
5. Every new rule gets a test that fails when the rule is removed, and an
   entry in `tests/mutation/run_mutations.py`.

## Running the tests

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q            # full suite (~15 s)
.venv/bin/python -m pyflakes custom_components tests
.venv/bin/python tests/mutation/run_mutations.py --out mutation_results.json   # ~15 min
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
| `tests/test_config_flow.py` | pre-existing wizard tests (with HA-version shim) |
| `tests/fusion_fixtures.py` | builders for the exact Fusion v0.3.3 attribute shape |
| `tests/mutation/run_mutations.py` | mutation record |

## Releasing

From 0.1.33.3 `hacs.json` has no `zip_release`: HACS installs the
`custom_components/open_meteo_solar_forecast/` folder straight from the tag.
A release is: bump `manifest.json` version, commit, create a GitHub release
with a new tag. No asset upload. (0.1.33.2 still needed the manually attached
zip; the inherited `release.yml` that built it is removed. Guarded by
`test_hacs_installs_from_the_tag_without_a_release_zip`.)

## Honest gaps

See §8 of the release audit and §7 of the test report. In short: no run on
a live Home Assistant with the real Fusion integration; the Fusion attribute
shape is reproduced from Fusion's source code, not captured from a live
instance; Home Assistant verified at 2026.2.3 only.
