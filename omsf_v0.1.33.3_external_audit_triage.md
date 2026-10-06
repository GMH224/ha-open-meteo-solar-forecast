# Open-Meteo Solar Forecast (GMH224 fork) v0.1.33.3 — External Audit Triage

**Input:** `ha_open_meteo_solar_forecast_0.1.33.3_ICS_OT_bug_audit.md` (external,
20 findings OMSF-001 … OMSF-020, dated 2026-10-06).
**Method:** no finding accepted on the auditor's word. Each was checked against
the code; 14 were reproduced by executable tests on a real Home Assistant
(pytest-homeassistant-custom-component, HA 2026.2.3) — file
`tests/test_zz_audit_repro.py` in the verification copy, assertions of the
*current* behaviour, all 14 passing. The remaining six were verified by code
reading. Severity was re-assessed for this installation and use (PV forecast
feeding dashboards and energy automations, single site, admin-controlled HA).

**Result:** 15 confirmed (severity adjusted on 8), 2 confirmed but by design /
documented, 1 disputed (impact confined to night hours), 2 declined. One
additional defect found during verification that the audit missed (V-1).

---

## 1. Verdict table

| ID | Auditor | Verdict | Our severity | Evidence | Origin |
|---|---|---|---|---|---|
| OMSF-001 | High | **Confirmed** | Medium | repro: with two entries the service changes only the last-loaded entry; service still registered after all entries unloaded | upstream (recorded as P-07 in 0.1.33.2, under-rated as Low) |
| OMSF-002 | High | **Confirmed** | Medium | repro: `latitude: "abc"`, `longitude: 999` persisted into the entry | upstream |
| OMSF-003 | High | **Confirmed** | Medium–High (OT) | code: no age limit on retained data in any mode | upstream (P-06) |
| OMSF-004 | High | **Confirmed, and broader (V-1)** | **High** | repro: hybrid with no complete day and Open-Meteo down returns an empty estimate as success | **own, 0.1.33.3** |
| V-1 | — (missed) | **New** | **High** | repro: hybrid with Open-Meteo down at 11:05 shows power now **0 W**; local mode on identical data shows > 1000 W. Today's partial local data is dropped by the whole-day join | **own, 0.1.33.3** |
| OMSF-005 | High | Confirmed, **documented design** (F8, L-5) | Low here (snow derating disabled, max = 0) | code | own, 0.1.33.2 |
| OMSF-006 | High | Confirmed, documented (F6) | **Low** — cell-temperature effect ≈ 0.4 %/K; fallback counted in diagnostics but not visible on the sensor | code | own, 0.1.33.2 |
| OMSF-007 | High | **Confirmed** | **High** (availability) | repro: one entry dated 2036 passes validation; synthesis span grows from ~700 to > 350 000 quarters (≈ 2 M solar-position evaluations per array per refresh) | own, 0.1.33.2 |
| OMSF-008 | High | **Confirmed** | Medium | repro: 50 000 entries parsed in 0.59 s on the event loop; no cap | own, 0.1.33.2 |
| OMSF-009 | High | **Partly confirmed** | Low–Medium | repro: azimuth 0.9/360.9 accepted (truncation); `inf` elevation accepted silently; `inf` azimuth → uncontrolled `OverflowError`; a directory → `IsADirectoryError`. File is admin-owned; failure is loud, not silent, except the `inf` elevation case | upstream |
| OMSF-010 | High | **Disputed (severity)** | Low | Timestamps are timezone-aware instants and stay correct across DST; only the *day grouping* uses the fixed offset. The misassigned hour is 00:00–01:00 local, where the sun is ≥ 30° below the horizon (repro for every night 25–31 Oct) → no effect on daily PV totals or curves. Sunrise/sunset are computed as instants and are unaffected. Documented as L-6; identical to Open-Meteo mode (library uses one `utc_offset_seconds`) | design, both modes |
| OMSF-011 | Med-High | **Confirmed** | Medium | repro: Open-Meteo day missing its 12:00 hour accepted as complete; total undercounted (4.5 instead of 5.0 kWh). With fixed-offset grouping every local day has exactly 24 hours, so "≥ 23" is simply wrong | own, 0.1.33.3 |
| OMSF-012 | Medium | **Confirmed** | Medium | repro: retained store with `watts: []` → `AttributeError` escapes → entry stuck in setup-retry although local data is usable | upstream path, all modes |
| OMSF-013 | Medium | **Confirmed** | Low | code: `_loaded = True` before the read; one failure disables history for the session | own, 0.1.33.2 |
| OMSF-014 | Med-High | Confirmed | Low | code: no array cap; arrays are admin-configured one page at a time | upstream |
| OMSF-015 | Medium | **Confirmed** | Low | repro: `supported_features: "abc"` → `ValueError` in the config flow (HA core normally guarantees an int) | own, 0.1.33.2 |
| OMSF-016 | Medium | **Not a defect** (hygiene) | — | repro: `inf` geometry raises `ValueError` before any computation. Accept the hygiene change (reject non-finite at parse) | own, 0.1.33.2 |
| OMSF-017 | Medium | **Declined** (documented) | Low | Custom `base_url` is a supported upstream feature (self-hosted Open-Meteo, often plain HTTP on a LAN). Admin-only; the API key is the user's own. Enforcing an allow-list would break legitimate installations | upstream |
| OMSF-018 | Medium | **Confirmed** | Medium | repro: aiohttp's `ClientResponseError` text contains the full request URL **including `apikey=`**. The library raises it for HTTP statuses it does not map (e.g. 500, 504). 0.1.33.2 put `str(err)` into the `forecast_source` attribute (recorded in the database). Not exposed on this installation (no API key) | **own, 0.1.33.2** |
| OMSF-019 | Low-Med | **Confirmed** | Low–Medium | repro: `wh_period_15m` not in the recorder exclusion; local/hybrid's 10-minute refresh triples the write rate vs. Open-Meteo mode | upstream, amplified by own |
| OMSF-020 | Medium | **Declined** | — | Multiple entries are intended (this installation runs three: East, South, West). A unique ID would block that. The real problem is the service (OMSF-001) | upstream |

---

## 2. Notes on the auditor's reasoning

Where the audit is strong: the trust-boundary findings (OMSF-007/008/012/018)
and the hybrid completeness rule (OMSF-011) are correct and were not caught
by our own 50-mutation record — the mutation runner only perturbs rules that
exist; it cannot find a rule that is missing (an input cap, an age limit).
That gap in our method is recorded.

Where the audit overstates:

- **OMSF-010 (DST)**: the audit reasons about local wall-clock labels; the
  data are aware instants. The only effect is the day assignment of one night
  hour.
- **OMSF-006**: "physically wrong" overstates a ±0.4 %/K effect; the remedy
  that matters is visibility, not removal (removing the fallback would leave
  quarters without temperature, which the library skips → 0 W, a worse
  failure).
- **OMSF-005**: correct observation, but behaviour is a documented decision
  and inactive on this installation. A bounded last-known-value hold is the
  proportionate fix.
- **OMSF-016**: the value is rejected before use; not a defect.
- **"Not ready for ICS/OT-hardened classification"**: agreed for the
  service, retention and input-bound findings; the overall framing ("better at
  producing a number than proving it is trustworthy") is fair for the
  inherited upstream paths and for V-1/OMSF-004 in our own hybrid code.

---

## 3. Proposed remediation — v0.1.33.4

**Own defects (fix, no owner decision needed):**

| Item | Fix |
|---|---|
| V-1 + OMSF-004 | Hybrid keeps a partial local day's data for intraday sensors (power now, this/next hour, remaining today) while its day total stays unknown — the same rule as local mode. A refresh that would produce no data at all is treated as a failure (retained / setup retry), never as success |
| OMSF-007/008 | Parser bounds: at most 500 entries; `period_start` within [now − 48 h, now + 10 days]; temperature list same bounds; out-of-window entries rejected and counted |
| OMSF-011 | Open-Meteo day complete only with all 24 hours (fixed-offset grouping) |
| OMSF-018 | Error texts sanitised before they reach attributes or diagnostics: URL query strings removed, length ≤ 200 |
| OMSF-013 | History store: retry the load on the next refresh after a failure (at most 3 attempts, then continue without) |
| OMSF-015/016 | Guarded `supported_features`; non-finite geometry rejected at parse |
| OMSF-005 | Snow: last valid value held up to 24 h, then treated as 0 with the existing flag |
| OMSF-006 | Data-quality attribute on `forecast_source` (`temperature: forecast / current_value_fallback`) |

**Inherited defects (touch Open-Meteo-mode code; need the owner's consent to
relax the "Open-Meteo mode unchanged" invariant for safety fixes):**

| Item | Proposed fix |
|---|---|
| OMSF-001/002 | Register the service once per integration with a schema: optional `config_entry_id`; coordinates finite and in range. With several entries and no id → error instead of silently changing the last-loaded entry |
| OMSF-003 | Bounded retention: retained forecast served at most N hours (proposal: 6 h), then sensors unavailable and `forecast_source` = `stale` |
| OMSF-009 | Horizon file: size cap, finite values, exact 0/360 bounds, controlled error for every I/O exception |
| OMSF-012 | Retained store: shape validation, discard on any malformation |
| OMSF-019 | Exclude `wh_period_15m` from the recorder |

**Declined:** OMSF-017 (documented), OMSF-020. **Disputed, documented:**
OMSF-010.

---

## 4. Final disposition (v0.1.33.4)

Owner decisions (6 Oct 2026): inherited findings fixed in all modes (the
"Open-Meteo mode unchanged" rule is relaxed for safety fixes); retained
forecast limit **6 hours**.

| ID | Disposition in 0.1.33.4 |
|---|---|
| V-1, OMSF-004 | Fixed as proposed |
| OMSF-001, 002 | Fixed as proposed. Behaviour change: with several entries the service now requires `config_entry_id` (this installation has three) |
| OMSF-003 | Fixed: 6 h limit, all modes; `forecast_source` = `stale` |
| OMSF-005, 006 | Fixed as proposed (24 h snow hold; `data_quality`) |
| OMSF-007, 008 | Fixed as proposed (500 entries; [now − 48 h, now + 10 d]) |
| OMSF-009 | Fixed **with one deviation from the proposal**: the endpoint rule (first azimuth truncates to 0, last to 360) is kept, because tightening it would break existing horizon files on upgrade and `numpy.interp` clamps outside the range. Everything else as proposed; the shipped sample files keep their classification |
| OMSF-011, 012, 013, 015, 016, 018, 019 | Fixed as proposed |
| OMSF-010 | Disputed; documented (L-6) |
| OMSF-014 | Not fixed (Low; admin-configured); backlog |
| OMSF-017, 020 | Declined; documented |

**Found while remediating (T-1, test harness):** the fake
`weather.get_forecasts` in the test suite keyed its response by the
characters of the entity id, so every integration test since 0.1.33.2 ran on
the temperature fallback; the forecast-temperature path was covered only by
unit tests (and verified live: 160/160 accepted on the owner's system). Fixed;
`test_happy_path_uses_the_forecast_temperatures` guards it.

**Method lesson.** Mutation testing measures whether existing rules are
tested; it cannot reveal a rule that does not exist. The audit's strongest
findings (input bounds, age limit, secret leakage, service lifecycle) were all
*missing* rules. DEVELOPER.md now lists boundary rules to check for every new
input.
