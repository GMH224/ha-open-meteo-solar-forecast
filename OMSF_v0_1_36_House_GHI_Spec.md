# Open-Meteo Solar Forecast (GMH224 fork) v0.1.36 — Measured light at the house (design record)

**Status:** confirmed by the owner on 16 Nov 2026 ("Let's go ahead"), after a proposal agreed with the weather
provider's team (same day). Interface for consumers: [`docs/house_ghi_interface.md`](docs/house_ghi_interface.md).
Also in this release: the same-day correction's fresh signal reaches the members before a decision (§9).

## 1. Purpose

Publish the light at the house as global horizontal irradiance (GHI), so a weather integration can use it as a
local ground truth: first only recorded, later possibly for a 1-hour nowcast and for learning its cloud-to-light
conversion. The PV installation is the sensor; all PV geometry stays inside this integration.

Owner requirements: horizontal, unobstructed W/m² (no solar geometry for the consumer); energy over a window, not
single readings; OPC-UA-style quality ("signal unavailable" when bad). Consumer requirements (16 Nov): fixed
attribute set; no forecast in the value; daily counts of bad windows by reason; full history in the export; fixed
references with a recorded version; 15-minute windows; uncertain values published with their reason.

## 2. Quantity

GHI (W/m²), mean of one 15-minute window aligned to :00/:15/:30/:45 UTC, from sunrise to sunset (sun above the
horizon at the middle of the window).

## 3. Method

1. **Energy.** Per inverter group, the window's energy from its DC energy counter (`same_day.band_energy`: step
   function, freshness by `last_reported`, plausibility bounds). A window waits up to 30 min after its end for
   readings past its end; then it is *bad* (`counter`).
2. **Response curves.** Each member's PV model (orientation, tilt, shading geometry incl. the fitted roof and
   chimney model, horizon file, efficiency, inverter limit) is run on clear-sky-shaped light at the grid of
   clear-sky indices k ∈ {0, 0.05, … 1.0, 1.05}: Haurwitz clear-sky GHI × k, diffuse split by Erbs, as for the
   same-day correction (`same_day.curve_records`). Temperatures: the station's measured hour means, else its
   latest sample (≤ 3 h old), else 10 °C (then *uncertain*, `temperature`). Rebuilt hourly. Summed over members
   and groups per window.
3. **Inversion.** k of the window = the k at which the summed curve reaches the summed measured energy
   (`same_day.invert`, linear between grid points; *saturated* at the top).
4. **GHI.** GHI = k × clear-sky GHI of the window (Haurwitz, mean of 5 instants at 1.5, 4.5, … 13.5 min).

The orientation of the arrays drops out in step 3: the value is the horizontal light that, through the house's
model, explains the measured energy.

## 4. Quality rules

First match wins; bad before uncertain. Parameters in `house_ghi.py`.

| Quality | Reason | Rule | Basis |
|---|---|---|---|
| bad | `counter` | any group without valid window energy | no measurement |
| bad | `startup` | window start < sunrise + 30 min | the 5K waits 30 min (owner data, Oct 2026) |
| bad | `low_sun` | elevation < 10°, or model clear-sky energy < 0.01 kWh | owner data: clear station, 08:00 (≈ 12°) the house gets 37 % |
| bad | `snow` | §6 | panels covered |
| bad | `clipping` | any group ≥ 95 % of its AC limit (largest limit of its members, as the forecast applies it: per-array limits added up, else `inverter_power`; 0.1.37.1, version 2) | 5K: 26 h at 4.8–5.0 kWh/h Jan–Nov 2026 |
| bad | `curtailment` | — (no export limit at this installation; owner 16 Nov) | interface completeness |
| uncertain | `clipping` | 85–95 % of the AC limit | near the limit |
| uncertain | `low_sun` | 10° ≤ elevation < 18° | owner data: 09:00 (≈ 17°) 76 %; ≥ 20° 0.85–1.0 |
| uncertain | `temperature` | no measured air temperature ≤ 3 h | model temperature assumed |
| uncertain | `saturated` | k at the top of the grid | lower bound only |
| uncertain | `resolution` | window clear-sky energy < 0.3 kWh | 0.01 kWh steps: \|Δk\| ≤ 0.033 per counter |
| uncertain | `mixed_sky` | Erbs diffuse fraction at kt = k × kt_clear in [0.25, 0.85] | direct/diffuse split assumed |

## 5. Versioning

`VERSION = 1`. The running version is recorded once with its start time (`version_history`, event
`house_ghi_version`). Any change of the clear-sky model, the model input or a rule raises it. The export gives each
window the version that produced it.

## 6. Snow

For each of the two complete hours before the window: house mean k (four windows), station k (station GHI ÷ the
same clear-sky GHI), air temperature, sun elevation. Snow if, in both hours, station k ≥ 0.5, temperature ≤ 3 °C,
elevation ≥ 18° and house k ≤ 0.4 × station k. If an hour cannot tell (missing data, dull station, low sun), the
day's previous window decides (snow stays until the house is seen bright again).

## 7. Storage, retention, export

Store section `house_ghi`: `windows` {start: [ghi, k, clear_ghi, quality, reason, elevation]} and
`version_history`. Strictly validated (a malformed section rejects the store as every other section). No schema
change: a store without the section gets an empty one. Retention 760 days, as the other measurements (about
1.5 MB per year). A full owner reset keeps the section: it is a measurement, not something learned. Export:
`house_ghi.csv`, `house_ghi_days.csv`, `settings.json` → `house_ghi`.

## 8. Sensor

`sensor.solar_forecast_learning_house_ghi` on the hub device; state and attributes as in the interface document.
Unavailable when the latest window is bad, older than 45 min after its end, or absent.

## 9. Same-day correction: fresh signal before the decision

Found in the review of 16 Nov: at the first decision (sunrise + 1 h) a valid signal existed on 5–8 Nov, but the
members' served forecast and the recorded decision still used the previous one (members refresh every 10 min).
Now, whenever a 5-minute run measures a valid signal, the hub refreshes every member and waits (≤ 60 s) before it
records decisions. The battery automation therefore reads a forecast at most 5 minutes behind the measurement, and
the same-day check judges what was actually served.

## 10. Implementation decisions

| # | Proposal (16 Nov) | Implemented | Why |
|---|---|---|---|
| 1 | mixed sky: k between 0.25 and 0.85 | Erbs diffuse fraction between 0.25 and 0.85 | the split matters when both direct and diffuse light are large; on grey days (k ≈ 0.3) the light is almost all diffuse and the orientation hardly matters |
| 2 | update 1–3 min after the window | extra runs at :02/:17/:32/:47 plus every 5 min; usually 2–6 min | the counters report a few seconds to minutes after a window ends |
| 3 | — | uncertain `resolution` | dim windows: one counter step is a large part of the energy |
| 4 | — | bad `low_sun` also when the model sees no light | inversion impossible |
| 5 | — | `bad_counts_today`, `uncertain_counts_today`, `windows_today`, `hour_start` added | consumer request 2; additive |
| 6 | — | full reset keeps the windows | a measurement history, as the consumer asked |
