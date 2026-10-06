# HA Open-Meteo Solar Forecast Integration

This custom component integrates the [open-meteo-solar-forecast](https://github.com/rany2/open-meteo-solar-forecast) with Home Assistant. It allows you to see what your solar panels may produce in the future.

> **GMH224 fork — v0.1.33.2.** Adds a **local weather source**: the forecast
> can be computed from Home Assistant entities (SwissWeather Fusion ≥ 0.3.3)
> instead of the Open-Meteo API, with no internet access. Open-Meteo mode is
> unchanged and remains the default. 175 tests (+144 subtests) on a real Home
> Assistant, physics validated against pvlib, 37/37 mutations caught.
> ICS documents:
> [architecture](OMSF_v0_1_33_2_Architecture_ICS.md) ·
> [release audit](omsf_v0.1.33.2_release_audit.md) ·
> [test report](omsf_v0_1_33_2_ICS_quality_bug_testing_report.md) ·
> [developer notes](DEVELOPER.md) · [changelog](CHANGELOG.md)

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rany2&repository=ha-open-meteo-solar-forecast&category=integration)

1. Go to the HACS page in your Home Assistant instance.
1. Search for `Open-Meteo Solar Forecast`.
   - If it doesn't immediately show up, check that the `Type` filter has `Integrations` ticked.
1. Install it.
1. Restart Home Assistant.

### Manual

1. Download the [latest release](https://github.com/GMH224/ha-open-meteo-solar-forecast/releases/latest) (for this fork; upstream: rany2/ha-open-meteo-solar-forecast).
2. Unpack the release and copy the `custom_components/open_meteo_solar_forecast` directory to the `custom_components` directory in your Home Assistant configuration directory.
3. Restart Home Assistant.

## Configuration

To use this integration in your installation, head to "Settings" in the Home Assistant UI, then "Integrations". Click on the plus button and search for "Open-Meteo Solar Forecast" and follow the instructions.

### Weather data source (local or Open-Meteo)

The first setup page has a **Weather data source** choice:

- **Open-Meteo API** (default): unchanged behaviour, refreshed every 30 minutes.
- **Local (Home Assistant entities)**: radiation and temperature come from
  entities in your Home Assistant. Refreshed every 10 minutes; no internet
  access.

With *Local*, a second page asks for:

| Field | Notes |
|---|---|
| Weather entity | e.g. `weather.swissweather_fusion_…`. Must provide an hourly forecast (temperature). |
| Irradiance series sensor | Leave empty for SwissWeather Fusion: `…_solar_irradiance_ghi_hour_average` is found automatically. Any sensor with an `hourly_forecast` attribute in Fusion's format works (hourly `period_start`/`period_end` in UTC, `ghi`/`dni`/`dhi` hour averages, optional `*_instant`). |
| Snow depth sensor | Optional, auto-detected for Fusion. Used only if *Maximum snow cover depth* is greater than 0. |
| Fall back to Open-Meteo | Off by default: if local data is missing or stale, the last good forecast is kept. On: Open-Meteo is queried instead. |

The radiation source is location-level; this integration computes the
tilted irradiance for each of your arrays (tilt, azimuth, trackers),
spreads hourly values to 15 minutes while conserving energy, and then runs
the same PV model as in Open-Meteo mode.

Things to know in local mode:

- `sensor.<name>_forecast_source` shows what produced the current forecast:
  `local`, `open_meteo`, `open_meteo_fallback` or `retained` (last good
  forecast kept because the source is unusable — see its `last_error`
  attribute).
- Days the local data does not fully cover are **unknown** rather than 0.
  With Fusion's ~5-day horizon, *5/6/7 days from now* are usually unknown.
  On the first day after installation, *today* is unknown until the
  integration has seen the whole day (Fusion does not publish past hours;
  the integration keeps them itself from then on).
- The radiation values are model output without bias correction.

Details: [architecture document](OMSF_v0_1_33_2_Architecture_ICS.md).

### Multiple PV Arrays

The setup wizard first asks for general settings (name, API details, inverter capacity), then shows one page per PV array with its location, orientation, power, tracking and shading settings. Tick "Add another array" to configure an additional array; repeat for as many arrays as you have.

To change the configuration later, open the integration's options and click "Next" after the general settings to edit each configured array. Location, orientation, module power, tracking and shading settings are on the array pages. Untick "Add another array" on an array page to drop the arrays after it.

Declination and azimuth accept fractional degrees (e.g. a declination of `22.5`).

### Azimuth

Azimuth ranges from 0° to 360°: North (0°), East (90°), South (180°), West (270°). For negative values, add 360° (e.g., -90° becomes 270°).

### Solar Tracking

The `tracking` option models panels that follow the sun instead of being fixed:

- `none` (default): fixed panels
- `azimuth`: vertical-axis (east-west) tracker; the configured azimuth is ignored
- `tilt`: tilt-axis tracker; the configured declination is ignored
- `dual`: dual-axis tracker; both azimuth and declination are ignored

The tracker type is set per array.

### Multiple Inverters

The "Inverter capacity" field in the general settings models a single inverter shared by all arrays: the combined output of all arrays is clamped to it (0 = no limit).

If each array is connected to its own inverter, set the "Array inverter capacity" field on the corresponding array pages instead. Each array's output is then clamped to its own inverter before the outputs are combined. Use 0 for arrays without a dedicated inverter (no limit for that array). As soon as any array has its own inverter capacity set, the shared inverter capacity from the general settings is ignored.

### DC Efficiency

The DC efficiency is the efficiency of the DC wiring and should not be confused with the cell efficiency. The DC efficiency is typically around 0.93. The cell efficiency is accounted for in the cell temperature calculation and is assumed to be 0.12.

### Damping Factor

The damping factor is a number between 0.0 and 1.0, where:
- **0.0:** No damping; panels produce maximum power
- **1.0:** Full damping; power is at minimum

For `damping_morning`, a factor of 1.0 causes power to start at 0 and increase steadily until midday `(sunrise + (sunset - sunrise) / 2)`.
For `damping_evening`, the same effect occurs in reverse, with power decreasing as the sun sets.

### Horizon Shading

A horizon profile text file accounts for direct sunlight blockage from obstacles (buildings, trees, etc.). The file contains two tab-separated columns of floats: azimuth (0° = north, 180° = south) and elevation angle (0° = flat horizon, 90° = directly overhead). Use a minimum of two lines with azimuth values strictly increasing from 0° to 360°; intermediate values are interpolated linearly.

**Note:** Store the file outside the custom_component directory to avoid overwriting during updates.

Use horizon enables/disables shading and takes effect immediately. It can be combined with damping factors.

Partial shading controls shadow estimation:
- **Disabled:** Only diffuse irradiation is used when a shadow is detected (suitable for far-away objects)
- **Enabled:** Shadows are treated as partial (suitable for close-by objects). An experimental calculation accounts for conditions by comparing diffuse/direct irradiation ratios; cloudy days behave as homogeneously shaded, while sunny days apply additional reductions.

For more information, see the [open-meteo-solar-forecast repository](https://github.com/rany2/open-meteo-solar-forecast).

## Credits

The [forecast_solar component code](https://github.com/home-assistant/core/tree/dev/homeassistant/components/forecast_solar) was used as a base for this integration. Thanks for such a clean starting point!
