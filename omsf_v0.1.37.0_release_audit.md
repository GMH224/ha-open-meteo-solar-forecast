# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.0 — Release Audit (ICS)

**Release type:** feature — Solcast as a second, independent light forecast, combined by a learned weight.
**Basis:** owner's request and decisions of 16 Nov 2026; comparison of free sources (Fusion already contains
the free high-resolution weather models; Solcast adds satellite cloud tracking); Solcast hobbyist terms and API
documentation.
**Design record:** `OMSF_v0_1_37_Solcast_Blend_Spec.md` (decisions in §8); architecture §0h (R57–R60, F37–F40).
**Invariants:** without a Solcast key nothing changes; with a key nothing served changes until the check passed and
the switch is on; Open-Meteo mode and entries outside a hub group untouched; no store schema change.

## 1. Owner decisions (16 Nov 2026)

| Question | Decision |
|---|---|
| Use of Solcast | an extra model for the production forecast, through the owner's own geometry |
| Site | one flat hobbyist site: tilt 0°, 1 kW DC/AC, efficiency 100 % (the owner's idea) |
| Integration | own API client in the hub (full control), not the HACS integration |
| Combination | learned weight starting at 0, "negated to zero" if Solcast adds nothing |
| Safety | never acts unproven: out-of-sample check, switch, nightly re-check |

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Engine (new, pure) | slots, budget, retry, answer validation, hour power, Solcast light, combination shift, pairs, factor and weight learning, out-of-sample check, store section, export | `solcast.py` |
| Hub | own HTTP client (5-minute run), member interface (`async_blend_plan`, `report_blend`), nightly learning and check, state and switch, export, full reset keeps the budget | `learning_hub.py` |
| Members | combination between the weather learning and the same-day correction; shadow estimate and report | `coordinator.py`, `runtime.py` |
| Store | section `solcast` (validated, copied in `view()`), switch setting, settings export without the key | `learning.py` |
| Configuration | hub page *Solcast* (key as a password field, site ID, limit) in setup and options | `config_flow.py`, `const.py`, translations |
| Entities | *Solcast combination*, *Solcast improvement*, switch *Solcast combination* | `learning_entities.py`, `switch.py` |
| Diagnostics | block `solcast`; key and site ID redacted | `diagnostics.py` |
| Docs | spec 0.1.37, architecture §0h, README, CHANGELOG, DEVELOPER (module map, rule 17) | |
| Version | 0.1.37.0 | `manifest.json` |

## 3. Soundness

| Claim | How it is ensured |
|---|---|
| "If Solcast adds nothing, its weight goes to zero" | least squares of the error of ours on the difference Solcast − ours; uncorrelated → ŵ ≈ 0; pulled to 0 by 60 hours; clamped ≥ 0. Tested on synthetic data: Solcast perfect → w ≈ n/(n+60); ours perfect → w < 0.05; equally good → about half |
| Light from the flat site is calibrated | c by least squares against the station, prior 1150 W/m² per kW with 30 hours; formula tested by hand |
| The check is out of sample | each judged day uses c and w learned only from the 28 days before it (M316); the first day therefore shows no difference (test) |
| No look-ahead in the live weights | nightly learning uses pairs before the run (M315) |
| The combination is physical | linear in the light; diffuse light combined with Erbs for Solcast's part; capped at 1.05 × clear sky; no change below 5° sun |

## 4. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Unproven model acts | shadow until check (≥ 14 days, ≥ 5 %, 5 of 7, energy not worse) and switch; nightly re-check switches it off |
| API budget and terms | 8 slots + 1 reserve within the limit; counted per UTC day and saved before sending; one retry only; no retry for authorisation errors; odd minutes |
| Secret handling | key only in the hub options (password field); redacted in diagnostics; absent from store, attributes and export (tests search all of them) |
| External faults | timeouts (30 s), network errors, HTTP errors, malformed answers: recorded, never raised into the forecast; ours used alone |
| Lock and event loop | the call runs in the hub's 5-minute run under its lock, bounded to 30 s; learning in the executor on a copy |
| Store size | answers 35 days (≈ 0.3 MB), pairs 400 days (≈ 2 MB), decisions 2 years |
| Interaction with the same-day correction | the correction measures the error of whatever is served; its learned persistence was learned without the combination (noted in §8) |

## 5. Verification

See `omsf_v0_1_37_0_ICS_quality_bug_testing_report.md`. Summary: 700 tests passing (+156 subtests), stable over
repeated runs; pyflakes clean; mypy 38 findings, the known set (none in new or changed code; one new finding was
fixed); privacy guard passing with `confidential.md` present. Mutations: new M300–M331 32/32 caught; regression on
the mutants of the changed modules 155/158 caught + 3 documented equivalent (M71, M117, M124), no regression.

## 6. Defects found in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | an empty key with a previously entered invalid site ID kept the Solcast page in error | flow test | empty key = Solcast off, the other fields are ignored (M331) |
| D-2 | a catch-up nightly run inside a hub test overwrote the test's learned weights (test only) | debugging a failing test | tests mark the nightly as done after setup |
| D-3 | a new helper returned `Any` (mypy) | mypy | typed |
| D-4 | the check's first draft learned only up to one day before the judged day (the last day before it was left out) | review | learns from all 28 days before the judged day |

## 7. Owner actions

1. Release `v0.1.37.0` (tag), HACS redownload, restart.
2. **Remove the HACS Solcast integration** (or disable its polling automation): both would use the same daily budget.
3. Hub options → page *Solcast*: API key, flat site ID, limit 10.
4. *Solcast combination* shows `collecting`; `calls_today` rises through the day (at most 9). After about two weeks
   *Solcast improvement* shows a value; switch on only when the state says `eligible`.

## 8. Honest gaps

- Not yet run against the real Solcast service; the client is tested against recorded answer formats.
- The same-day correction's persistence was learned on the forecast without the combination; once the combination
  acts, that table re-learns over the following weeks.
- The free answer has no direct/diffuse split; Erbs is used for Solcast's part.
- The energy half of the check uses the energies recorded live (with the weights of that day), not recomputed out
  of sample; the light half is fully out of sample.
- Solcast's terms foresee sites that describe one's own system; a flat reference site at the home is a reasonable
  reading but not explicitly covered — the owner may confirm with Solcast.
