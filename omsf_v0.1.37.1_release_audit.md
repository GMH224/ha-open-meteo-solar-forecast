# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37.1 — Release Audit (ICS)

**Release type:** maintenance — remediation of the independent review of 0.1.37.0.
**Basis:** `OMSF_v0_1_37_0_ICS_Quality_Review.md` (17 Nov 2026; source-based, reviewed archive SHA-256
`a10eb285…e05f` = the 0.1.37.0 release). The review is treated as input, not as truth: every finding was
reproduced in the source and by a test before it was fixed; severity and impact were assessed independently.
**Owner decision (17 Nov 2026):** fix ICS-01 to ICS-07 as one maintenance release, with tests, mutation testing,
documentation and this audit.
**Invariants:** no new function; no store schema change; for a complete day, a valid store and a shared inverter
limit the forecast, the learning and the house light are unchanged; Open-Meteo mode untouched.

## 1. Answer to the review, finding by finding

| ID | Review | Verified | Our assessment | Fix | Tests | Mutations |
|---|---|---|---|---|---|---|
| ICS-01 | P2 — partial model days skipped, later backfill never modelled, day accepted without ratio check | **yes, wider than stated** | **P2.** No late data is needed: one missing station daylight hour already leaves its model hour empty for ever, and after 3 days the day was accepted with `ratio_unchecked`. A day with local clouds could so enter the learning. Not active for the owner (all 34 decided days complete) | model every hour with station light that is missing in either map (hours already modelled stay); judge the ratio over the hours with station light and model, ≥ 80 % of daylight; wait while an hour with station light lacks its model | 7 | M332–M339 |
| ICS-02 | P2 — clipping ignores per-array inverters; "can influence the learned House-GHI curve" | yes, in code | **P3.** The impact statement is not correct: the house light feeds **no** learning in this integration (rule 16; it is an output for Fusion). Effect: a quality flag too good in per-array setups. Not active for the owner (shared limits only) | `house_ghi.effective_ac_limit_w`: the forecast's precedence (per-array limits add up and override the shared one; an array without its own inverter → no total limit; else shared); `house_ghi.VERSION` 2 (rule 16) | 18 | M340–M347 |
| ICS-03 | P3 — invalid JSON bypasses failure count and retry | yes | **P3.** Caught by the step guard, never reaches the forecast; only the failure accounting was wrong | HTTP 200 with a body that is not JSON → failure, one retry (cut-off answers are usually transient) | 3 | M348–M350 |
| ICS-04 | P3 — Solcast gate `days` not validated | yes, **wider than stated** | **P3.** The same gap existed for `improvement` and for the stage-1 and same-day check results. Only through corruption or manual editing | `learning.gate_problem` validates every field read back (`passed`, `days`, `better_recent`, `improvement`, `reasons`) of all three checks and their histories; a malformed result is **dropped** (event `store_repaired`, re-evaluated the next night; the learning it guards is not applied meanwhile) instead of quarantining the whole store | 28 | M351–M359 |
| ICS-05 | P3 — naive retained timestamp aborts the first refresh | yes, **effect larger** | **P3.** The cause is rare (our code always writes the zone), but the effect is not one failed refresh: every setup retry reads the same store, so the entry **failed repeatedly** until the store was deleted | a stored time without zone discards the retained forecast; a fresh forecast is fetched | 4 | M360 |
| ICS-06 | P3 — snow hold lost on restart | yes | **P3**, rare (sensor must stay unavailable after the restart). Not active for the owner (snow derating off) | last valid depth stored with time and sensor; restored only for the same sensor, with zone, not in the future (5 min slack), in range; the hold still ends 24 h after the original reading | 16 | M361–M367 |
| ICS-07 | P3 — shading cache misses same-size/same-time changes | yes | **P4 / informational.** Normal editing changes the time; only tools that copy timestamps trigger it; a restart clears it | cache keyed by the SHA-256 of the content; the file is always read (≤ 64 KiB), parsed only when the content changed | 3 | M368–M370 |

**Section 4 of the review (observations):** agreed. 4.1 (DST grouping) was assessed in the 0.1.33.3 triage and
does not change the PV totals at this latitude; 4.2 (shading index relies on the library's request order) is a
documented compatibility dependency, re-checked at every library update.

**Section 5.2 (full suite not run by the reviewer):** the reviewer's environment lacked the test dependency. The
complete suite was run here (section 4); the dependency is pinned in `requirements-test.txt`.

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Learning hub | nightly model fills missing hours (ICS-01); clipping limits through the forecast's rule (ICS-02); body-not-JSON error with retry (ICS-03); `store_repaired` event and warning (ICS-04) | `learning_hub.py` |
| Learning engine | day check over the hours with station light and model, detail `ratio_hours` (ICS-01); `gate_problem`, `LearningData.repaired` (ICS-04) | `learning.py` |
| House light | `effective_ac_limit_w`; `VERSION` 2 with note (ICS-02) | `house_ghi.py` |
| Solcast engine | gate check moved to the common validation (ICS-04) | `solcast.py` |
| Members | retained time must carry its zone (ICS-05) | `coordinator.py` |
| Local source | snow hold stored and restored (ICS-06) | `local_provider.py` |
| Shading | content-keyed cache (ICS-07) | `shading_provider.py` |
| Tests | new `tests/test_v0_1_37_1_audit_remediation.py` (79); one case moved in `test_solcast.py` (a bad Solcast gate is now dropped, not a section error) | |
| Docs | CHANGELOG, architecture §0i (R61–R67, F41–F43), DEVELOPER rule 18 and test table, house-light spec and interface (version 2) | |
| Version | 0.1.37.1 | `manifest.json` |

## 3. Safety analysis (ICS)

| Concern | Assessment |
|---|---|
| Can the fixes make the forecast worse? | No path of the served forecast changes for valid data. ICS-01 only adds hours to a model and *adds* a ratio check where 0.1.37.0 made none; a day with complete station data gets exactly the same decision (same hours, same ratio). ICS-04 can only switch learning *off* until the next nightly check, never on |
| Fail-safe direction | malformed derived data → dropped and recomputed; implausible stored snow → ignored (behaviour of 0.1.37.0); naive retained time → fresh fetch; unknown inverter topology (an array without limit) → no clipping check, as before |
| Store compatibility | no schema change; existing stores load unchanged (the owner's 8 stored check results from the diagnostics pass `gate_problem`); house light records version 2 with its date on the first run |
| Owner's installation | none of the seven was active (complete station days, shared inverter limits, snow derating off, valid store). Visible changes: house-light `version` 2 (values identical), a possible `ratio_hours` detail on future days with a station gap |
| Secrets | the new Solcast error text names only the exception type, never the body or the key (test) |

## 4. Verification

See `omsf_v0_1_37_1_ICS_quality_bug_testing_report.md`. Summary: 779 tests passing (+156 subtests), identical in two consecutive runs; pyflakes clean; mypy 38 findings, the known set (one new finding in the first draft was fixed); privacy guard passing with `confidential.md` present. Mutations: New M332–M370: 38/39 caught + 1 equivalent (M364); M333 and M347 escaped at first and each closed a test gap. Regression on every earlier mutant (M01–M331) in the seven changed modules: 212/215 caught + the 3 documented equivalent (M71, M117, M124), no regression. M87 and M209 targeted code that ICS-07 / ICS-01 replaced; both were retargeted to the new code and are caught.

Each new test was run against a copy of 0.1.37.0: 60 of the 78 first tests failed there (every finding has at
least one); the 18 that pass on both are guards (unchanged behaviour, implausible input ignored, bounds).

## 5. Defects found in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | the first draft of the check validation still allowed `days: null`, which the Solcast state compares with a number | review of the consumers | counts must be non-negative integers |
| D-2 | a malformed Solcast gate would have quarantined the whole learning store (strict section validation) | design review | gate check moved to the common validation, which drops only the result |
| D-3 | mypy: history type after a `_require` | mypy | explicit check |
| D-4 | two new tests mixed the clocks of two test modules / tripped the counter-fault limit (tests only) | first test run | corrected |
| D-5 | mutations M333 and M347 escaped: an hour in one model map only, and a multi-array entry with one inverter value, were not tested | mutation run | tests added |

## 6. Owner actions

1. Release `v0.1.37.1` (tag), HACS redownload, restart. No configuration change.
2. Nothing to watch beyond the usual: the house light's `version` attribute shows 2 (Fusion: values unchanged,
   see `docs/house_ghi_interface.md` §5).

## 7. Honest gaps

- ICS-02: with per-array inverters, the check works on the group's total; one array clipping while the total
  stays below the summed limits is not detected (the counters measure the total only).
- ICS-01: an hour without station light is left out of the ratio; a day with 20 % of its daylight missing at the
  station is still accepted unchecked after the wait, as before (it passed the station minimum).
- ICS-06: a stored snow value is trusted for the rest of its 24 h even if the snow melted during a long restart;
  unchanged from the running behaviour.
- The real Solcast service is still not called in tests (see 0.1.37.0).
