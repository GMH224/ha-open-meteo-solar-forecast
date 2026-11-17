# Interface: measured light at the house (`house_ghi`)

**Provider:** Open-Meteo Solar Forecast (GMH224 fork), learning hub, from 0.1.36.0
**Interface version:** 2 (attribute `version`; 2 from 0.1.37.1, see section 5)
**Audience:** consumers of a local irradiance sensor, e.g. SwissWeather Fusion ("local irradiance sensor (W/m²)")

## 1. What it is

Global horizontal irradiance (GHI) in W/m² — the light on 1 m² lying flat and unobstructed — averaged over one
15-minute window, measured at the house. It is derived from the PV inverters' energy counters; all PV and solar
geometry stays inside the provider. The meaning is the same as a weather station's global radiation.

## 2. Entity

| Item | Value |
|---|---|
| Entity id | `sensor.solar_forecast_learning_house_ghi` |
| Device class / unit | `irradiance`, `W/m²` |
| State class | `measurement` (Home Assistant keeps hourly long-term statistics) |
| State | GHI of the **last complete 15-minute window**, rounded to 0.1 W/m²; `unavailable` when that window's quality is *bad*, when there is no window yet today, or when the latest window ended more than 45 min ago |
| Update | at about hh:02, hh:17, hh:32, hh:47 and every 5 minutes; a window is assessed as soon as both inverters' counters have reported past its end (normally within 2–6 min), at the latest 30 min after its end (then *bad*, `counter`) |

Home Assistant shows no attributes while the state is `unavailable`. *Bad* windows are therefore visible in
`recent` of the next published window, in the daily counts and in the export (section 6).

## 3. Attributes

| Attribute | Type | Meaning |
|---|---|---|
| `quality` | `good` \| `uncertain` \| `bad` | quality of the window in the state |
| `reason` | string \| null | why *uncertain* or *bad* (section 4); null when *good* |
| `window_start`, `window_end` | ISO 8601, UTC | the window the state describes; align by these, not by the update time |
| `clear_sky_index` | float \| null | k = GHI ÷ clear-sky GHI (0 … 1.05) |
| `clear_sky_ghi` | float | clear-sky GHI of the window, W/m² (Haurwitz, mean of 5 instants) |
| `hour_ghi` | float \| null | mean GHI of the last complete clock hour (4 windows); null if any of them is *bad* or missing |
| `hour_quality` | string | worst quality of those 4 windows (`bad` when one is missing) |
| `hour_start` | ISO 8601, UTC | start of that hour |
| `recent` | list | the last 8 windows: `[window_start, ghi or null, quality]`, oldest first |
| `version` | integer | interface/method version (section 5) |
| `windows_today` | integer | windows assessed today (sunrise to now) |
| `bad_counts_today` | mapping | *bad* windows today by reason: `counter`, `startup`, `low_sun`, `snow`, `clipping`, `curtailment` |
| `uncertain_counts_today` | mapping | *uncertain* windows today by reason |

### Example

```yaml
state: 142.3
attributes:
  unit_of_measurement: W/m²
  device_class: irradiance
  state_class: measurement
  quality: good
  reason: null
  window_start: "2026-11-16T09:00:00+00:00"
  window_end: "2026-11-16T09:15:00+00:00"
  clear_sky_index: 0.4112
  clear_sky_ghi: 346.1
  hour_ghi: 151.0
  hour_quality: uncertain
  hour_start: "2026-11-16T08:00:00+00:00"
  recent:
    - ["2026-11-16T07:15:00+00:00", null, "bad"]
    - ["2026-11-16T07:30:00+00:00", 41.2, "uncertain"]
    - ["2026-11-16T09:00:00+00:00", 142.3, "good"]
  version: 2
  windows_today: 9
  bad_counts_today: {counter: 0, startup: 2, low_sun: 1, snow: 0, clipping: 0, curtailment: 0}
  uncertain_counts_today: {clipping: 0, low_sun: 4, temperature: 0, saturated: 0, resolution: 0, mixed_sky: 1}
```

## 4. Quality and reasons

The first matching rule decides; *bad* rules are checked before *uncertain* ones.

| Quality | Reason | Condition |
|---|---|---|
| bad | `counter` | an energy counter has no valid reading for the window (missing, stale, negative or implausible step) |
| bad | `startup` | window starts less than 30 min after sunrise (an inverter waits for light) |
| bad | `low_sun` | sun below 10° at the middle of the window, or the model sees no light (sun behind modelled obstacles) |
| bad | `snow` | snow on the panels suspected: in each of the last two complete hours the reference station saw a bright sky (k ≥ 0.5), air temperature ≤ 3 °C, sun ≥ 18°, and the house got ≤ 40 % of the station's k; when the station cannot tell, the day's previous window decides |
| bad | `clipping` | an inverter at ≥ 95 % of its AC limit over the window |
| bad | `curtailment` | export limit active — part of the interface; this installation has no export limit, so it never occurs |
| uncertain | `clipping` | an inverter at 85–95 % of its AC limit |
| uncertain | `low_sun` | sun between 10° and 18° (nearby trees and buildings not yet learned) |
| uncertain | `temperature` | no measured air temperature in the last 3 h; 10 °C assumed |
| uncertain | `saturated` | the measured energy is at or above the model's clearest sky (k = 1.05): "at least this bright" |
| uncertain | `resolution` | clear-sky energy of the window below 0.3 kWh (counter steps of 0.01 kWh matter) |
| uncertain | `mixed_sky` | diffuse fraction of the window (Erbs, from k) between 0.25 and 0.85: the direct/diffuse split is assumed |
| good | — | none of the above |

`ghi` and `clear_sky_index` of *bad* windows: `ghi` is never given; `clear_sky_index` may be given for diagnosis
(snow, clipping) and must not be used.

## 5. Independence and versions

- **No forecast input.** The model is run on synthetic clear-sky-shaped light (Haurwitz clear sky scaled by k,
  diffuse split by Erbs) with the reference station's **measured** air temperature. No weather forecast (light,
  cloud, temperature), no learned forecast correction, no calibration factor and no same-day correction enters
  the value. The value may therefore be used to learn a forecast's errors.
- **Fixed references.** Clear-sky model (Haurwitz), PV model input and the quality rules are fixed per `version`.
  Every change raises `version`; the provider records each version with its start date (export, section 6).
  - Version 1 (0.1.36.0): first version.
  - Version 2 (0.1.37.1): the `clipping` rule uses the inverter limits the PV forecast uses (per array when set,
    otherwise the shared limit). With a shared inverter limit, values and qualities are identical to version 1.
  - Planned: learning the house's low-sun losses (expected early 2027, version 3), which will narrow the
    `low_sun` rules.

## 6. History

Home Assistant's recorder keeps the full state history only for its retention period (10 days by default), and
long-term statistics keep hourly means only. The provider therefore keeps **every window for two years** in its
own store and writes it every night (and on demand, action `open_meteo_solar_forecast.export_learning`) to
`/config/open_meteo_solar_forecast/export/`:

- `house_ghi.csv` — one row per window from sunrise to sunset:
  `window_start_utc, window_end_utc, ghi_w_m2, clear_sky_index, clear_sky_ghi_w_m2, quality, reason,
  sun_elevation_deg, version`
- `house_ghi_days.csv` — per day: `date, windows, good, uncertain, bad`, then `bad_<reason>` for every bad reason
  and `uncertain_<reason>` for every uncertain reason
- `settings.json`, section `house_ghi` — `version`, `version_history` (`[start, version, description]`) and the
  rule parameters

## 7. What to expect

Checked on the owner's data of 19 Oct – 15 Nov 2026 (the same k, from the provider's same-day measurement), hourly
means against the reference station about 8 km away:

| Sun elevation | Hours | House ÷ station | Correlation | Mean abs. difference |
|---|---|---|---|---|
| ≥ 18° | 150 | 0.93 | 0.91 | 40 W/m² (station mean 181) |
| 10–18° | 63 | 0.81 | 0.89 | 28 W/m² (station mean 93) |

Part of the difference is real (8 km apart, passing clouds), part is the PV model at low sun (obstacles near the
house). In December and January most windows are *uncertain* (`low_sun`) because the sun stays below 18° or close
to it.

## 8. Contract

- The entity id, the attribute names above and the meaning of `quality`/`reason` are stable within a `version`.
  New attributes may be added; none is removed or renamed without raising `version`.
- Times are UTC. Windows are aligned to :00, :15, :30, :45.
- Contact: the provider's issue tracker (GitHub GMH224/ha-open-meteo-solar-forecast).
