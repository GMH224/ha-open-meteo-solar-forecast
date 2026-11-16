# Open-Meteo Solar Forecast (GMH224 fork) v0.1.37 — Solcast as a second light forecast (design record)

**Status:** confirmed by the owner on 16 Nov 2026 ("ok, let's build it"), after: the flat reference site (owner's
idea), "if Solcast adds nothing, the learning should negate it to zero", "call the API ourselves for full control",
and "it cannot make it worse than we currently have" (answered in §6).

## 1. Purpose

Add an independent forecast to the weather forecast the members use today (SwissWeather Fusion, after the weather
learning). Fusion blends weather models (MeteoSwiss ICON-CH1/CH2, DWD ICON-D2, Météo-France AROME); Solcast adds
satellite-based cloud tracking, strongest for the next hours. The combination is learned from the owner's data
and acts only when it measurably helps.

## 2. The flat reference site

A Solcast hobbyist rooftop site at the owner's home: tilt 0°, 1 kW DC, 1 kW AC (never clips), efficiency 100 %.
Its power forecast is the light on the flat (GHI) passed through Solcast's own empirical PV model. All geometry
of the real arrays (three orientations, roof and chimney shading, horizon, inverter limits) stays in this
integration's model.

## 3. Calls (own client, full control)

| Item | Rule |
|---|---|
| Endpoint | `GET https://api.solcast.com.au/rooftop_sites/{site}/forecasts?format=json&hours=72`, `Authorization: Bearer <key>` |
| Settings | hub options page *Solcast*: API key (password field), site ID, daily limit (default 10); an empty key turns Solcast off |
| Slots | local 05:07, 09:37, 11:07, 12:37, 14:07, 15:37, 20:37 and sunrise + 40 min (just before the battery decision at sunrise + 1 h); odd minutes avoid the full hour, when Solcast is busiest |
| Due | within 45 min after the slot, once per slot and local day (checked every 5 minutes) |
| Budget | per UTC day (as Solcast counts); planned calls stop at limit − 1; the last call is kept for one retry; a call is counted and saved **before** it is sent, so a restart never uses extra calls |
| Errors | HTTP 429 or 5xx, timeouts, network errors: one retry after 5–15 min (random) if budget is left; 401/403/404 and unusable answers: no retry; after 3 failures in a row an event; the forecast runs on without Solcast |
| Answer | half-hour periods (estimate, 10 %, 90 %, kW of the flat 1 kWp site) from now to 54 h; validated (numbers, 0–1.5 kW, ordered p10 ≤ p ≤ p90, half-hour aligned); kept 35 days |
| Use | the newest answer, if not older than 12 h; hour value = mean of its two half hours |
| Secrets | the key and site ID live only in the hub's options; redacted in diagnostics; never in the store, the export or an attribute |

## 4. Light from Solcast

GHI_s = c × P (W/m²), P = the flat site's power (kW). Clearness kt_s = GHI_s / E0h, limited to 1.05 × the
Haurwitz clear-sky clearness; none when the sun is below 5°. The factor c is learned nightly by least squares
through the origin on the shortest lead bucket (0–3 h) against the reference station's measured GHI, over the
last 28 days, with 30 prior hours at the physical value 1150 W/m² per kW (≈ 87 % system efficiency), clamped to
800–1600; at least 20 hours, else the prior.

## 5. Combination

Per hour, on the clearness of the light the members would use without Solcast (`kt_ours`: after the weather
learning when it is applied):

kt_blend = kt_ours + w_b × (kt_s − kt_ours), w_b ∈ [0, 1]

- One weight per lead bucket b: 0–3, 3–6, 6–18, 18–30, 30–54 h (lead to the middle of the hour, from the moment of
  serving).
- w_b by least squares of (kt_station − kt_ours) on (kt_s − kt_ours) over the last 28 days, then pulled towards
  0 by 60 prior hours (w = ŵ × n / (n + 60)), clamped to [0, 1]. Without Solcast's help ŵ ≈ 0, so w ≈ 0: "ours
  only".
- Diffuse part: the diffuse light is combined too; Solcast's free answer has no split, so its diffuse fraction is
  Erbs at kt_s.
- Applied through the same record rebuild as the weather learning (`apply_corrections`, no stage-2 factor).
- Order: weather learning → **combination** → same-day correction → served.
- Learning data: per hour and bucket one pair (kt_ours, P, P10, P90, issue time), the one nearest the bucket's
  centre (1.5, 4.5, 12, 24, 42 h), recorded at member refreshes; kept 400 days.

## 6. Check, switch, and "never worse than today"

| Rule | Value |
|---|---|
| Served only if | configured, check passed, owner's switch *Solcast combination* on, no configuration problem |
| Until then | shadow: computed and recorded, forecasts unchanged |
| Check (nightly) | the decision forecast (first member refresh at or after sunrise + 1 h) of each of the last 28 days, combined with c and w learned **only from the 28 days before that day** (out of sample) |
| Pass | ≥ 14 such days with Solcast covering at least half the lit hours; light RMSE ≥ 5 % lower; better on ≥ 5 of the last 7 days; per inverter group the day-energy error not worse (1 % tolerance) on ≥ 7 days |
| Switch-off | the check runs every night; when it fails the combination stops by itself |
| Damage limits | w ≤ 1 and pulled to 0; missing, old or broken Solcast data: ours alone for that hour; any failure: forecast as without Solcast |

On a single day the combination can be worse; on average it acts only while measured better out of sample.

## 7. Store, entities, export

- Store section `solcast` (strictly validated; no schema change): calls, slots, retry, failures, answers, pairs,
  decisions (hours and members' energies), learned blend, check and history. A full owner reset keeps the call
  budget and the answers, clears the learned part and the switch.
- Entities: *Solcast combination* (state not_configured / collecting / shadow / eligible / applied / paused;
  attributes weights, factor, calls today, last call/success/error), *Solcast improvement* (%), switch *Solcast
  combination*.
- Export: `solcast_pairs.csv`, `solcast_decision_hours.csv`, `solcast_decision_energy.csv`, `solcast_issues.csv`,
  `settings.json` → `solcast` (no key).

## 8. Implementation decisions

| # | Discussed | Implemented | Why |
|---|---|---|---|
| 1 | Solcast as another analog predictor | learned weight | an irrelevant predictor does not drop to zero in the analog distance |
| 2 | HACS integration | own client | timing (sunrise + 40), raw archive, no dampening, traceable budget |
| 3 | 9 calls a day | 8 planned slots + 1 retry in reserve (limit 10) | one retry for a busy server without exceeding the limit |
| 4 | — | energy rule judged on the live-recorded energies | a member's day energy needs its model; the light part is fully out of sample |
