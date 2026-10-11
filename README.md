# HA Open-Meteo Solar Forecast Integration

This custom component integrates the [open-meteo-solar-forecast](https://github.com/rany2/open-meteo-solar-forecast) with Home Assistant. It allows you to see what your solar panels may produce in the future.

> **GMH224 fork — v0.1.33.4.** Adds a **local weather source**: the forecast
> can be computed from Home Assistant entities (SwissWeather Fusion ≥ 0.3.3)
> instead of the Open-Meteo API, with no internet access — and, from 0.1.33.3,
> a **hybrid** mode: local for every day the local data covers, Open-Meteo for
> the rest. Open-Meteo mode is unchanged and remains the default.
> ICS documents:
> [architecture](OMSF_v0_1_33_2_Architecture_ICS.md) ·
> release audits [0.1.33.4](omsf_v0.1.33.4_release_audit.md), [0.1.33.3](omsf_v0.1.33.3_release_audit.md), [0.1.33.2](omsf_v0.1.33.2_release_audit.md) ·
> [external audit triage 0.1.33.3](omsf_v0.1.33.3_external_audit_triage.md) ·
> test reports [0.1.33.4](omsf_v0_1_33_4_ICS_quality_bug_testing_report.md), [0.1.33.3](omsf_v0_1_33_3_ICS_quality_bug_testing_report.md), [0.1.33.2](omsf_v0_1_33_2_ICS_quality_bug_testing_report.md) ·
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
- **Hybrid (local, Open-Meteo beyond)** *(0.1.33.3)*: every day the local data
  covers completely comes from it; every other day comes from Open-Meteo.
  The switch happens at midnight, so no day mixes two sources. Each day sensor
  has a `source` attribute (`local` / `open_meteo`).

With *Local* or *Hybrid*, a second page asks for:

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
- In **Local** mode, days the local data does not fully cover are **unknown**
  rather than 0 (some dashboard cards draw unknown as 0.0). On the first day
  after installation, *today* is unknown until the integration has seen the
  whole day (Fusion does not publish past hours; the integration keeps them
  itself from then on). **Hybrid** mode fills these days from Open-Meteo.
- In **Hybrid** mode the Open-Meteo days use the model set on the first page.
  MeteoSwiss models end after 5 days; for days 6–7 use `best_match`.

### Stale data (all modes, 0.1.33.4)

If no fresh forecast can be obtained, the last good one is kept for at most
**6 hours**. After that the forecast sensors become *unavailable* and
`sensor.<name>_forecast_source` shows **stale** (its `last_error` attribute
says why). The source sensor's `data_quality` attribute shows degraded inputs
in local/hybrid mode (temperature fallback, snow depth held or assumed 0).

### Service `update_array_location`

Sets an entry's location (default: the Home Assistant home location). With
more than one entry configured, `config_entry_id` must be given; coordinates
are validated (finite, latitude −90…90, longitude −180…180).
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

### Shading (partial shading by nearby objects, 0.1.33.5)

The horizon file treats shade as all-or-nothing for the whole array. Close
objects - another wing of the house, a chimney, a dormer, a tree - usually
shade only *part* of a module field, and because modules are wired in
strings with bypass diodes, a partly shaded field loses more than the shaded
area. For this, every array has an optional **Shading file** field. Two
kinds of file are accepted; the file name decides which:

| File | What it contains | When to use |
|---|---|---|
| `*.yaml` / `*.yml` - **geometry** | the module field (orientation, position, rows x columns, module size, bypass diodes, cell type, strings) and the objects around it | you can describe the surroundings with a few boxes, buildings, walls and trees |
| anything else (`.txt`, `.csv`) - **shading table** | for each sun position the share of direct light that still produces power: `azimuth  elevation  factor` (optionally `month` first) | you have measured data or another tool, or you want to edit the result of a geometry |

What the integration does with it, identically for Open-Meteo, local and
hybrid data: every 15 minutes the **direct** sunlight on the panel plane is
derived from the forecast's direct radiation and the sun position, and only
the blocked share of it is removed. Diffuse light is never reduced. Arrays
without a file behave exactly as before. A geometry file is ray-traced once
when the entry is loaded (typically well under a second) and cached; a
changed file is read again on the next reload.

A faulty file never stops the forecast: that array runs without shading,
Home Assistant shows a **repair issue** with the reason, the
`forecast_source` sensor shows `shading: [{state: error, ...}]`, and the
setup/options dialog refuses the file with the same message.

#### Geometry file (version 1)

Frame: metres, **x = east, y = north, z = up**, any origin (a house corner
at eave height is convenient). Angles: compass degrees (0 = north,
90 = east, 180 = south). One file describes **one** module field.

```yaml
version: 1
electrical_model: bypass      # bypass (default) | area (optimisers / micro-inverters)
array:
  azimuth: 197                # direction the panels face
  tilt: 45
  origin: [-5.8, 2.0, 0.25]   # lower-left corner of the module field, seen from the front
  rows: 5                     # row 1 = bottom
  columns: 3                  # column 1 = left
  module_width: 1.72          # along a row (width > height = landscape)
  module_height: 1.13         # up the slope
  gap: 0.02                   # optional
  bypass_diodes: 3            # optional
  cell_type: full             # full | half_cut
  strings:                    # optional; default: all modules in one string
    - [r1c1, r1c2, r1c3, r2c1, r2c2, r2c3, r3c1, r3c2, r3c3, r4c1, r4c2, r4c3, r5c1, r5c2, r5c3]
obstacles:
  - type: gable_building      # another wing, a neighbour
    center: [5.5, 0.2]
    ridge_azimuth: 358
    length: 18
    width: 11
    eave_height: 0
    ridge_height: 5.5
  - type: box                 # chimney, dormer
    center: [2.1, 3.5]
    width: 0.7
    depth: 0.7
    azimuth: 107              # direction of the width axis
    bottom: 0
    top: 5.9
```

Obstacle types: `box`, `gable_building`, `polygon` (any flat shape given by
its corners, e.g. a wall or fence; corners must lie in one plane),
`cylinder` and `cone` (trees, masts; `radius_top` makes a truncated cone).
Every obstacle accepts an optional `name` and `transparency` - a number
0...1 or a list of 12 monthly values for deciduous trees (see
[`examples/shading/example_all_types.yaml`](examples/shading/example_all_types.yaml)).
Unknown keys, values out of range and obstacles that overlap the module
field are refused with a message naming the exact field.

Electrical models: **bypass** - each bypass-diode section is limited by its
darkest cell (half-cut modules: by the mean of their two halves), shaded
sections are bypassed and each string runs at its best operating point;
**area** - loss proportional to the shaded area (module-level optimisers).

#### Shading table

```text
# azimuth  elevation  factor        (tab, comma, semicolon or space separated)
0          0          1.0
...
96         25         0.0
108        33         0.79
```

A complete regular grid is required (every azimuth with every elevation).
With a fourth leading column `month` (1-12), all 12 months must be present.
Between grid points the factor is interpolated; outside the azimuth range of
a partial table and above its highest elevation the factor is 1 (unshaded),
below its lowest elevation the lowest row applies. A table covering 0...350
in equal steps wraps around north.

`tools/shading_table.py my_roof.yaml > my_roof_table.txt` writes the table a
geometry produces (no Home Assistant needed; `--check` only validates).

Limits: file 256 KiB, 70 000 table rows, 64 obstacles, 64 polygon corners,
120 modules, 16 strings, coordinates within ±1000 m. Not supported for
tracking arrays. Shadows on diffuse light (the part of the sky an object
hides) and reflections are not modelled.

## Credits

The [forecast_solar component code](https://github.com/home-assistant/core/tree/dev/homeassistant/components/forecast_solar) was used as a base for this integration. Thanks for such a clean starting point!
