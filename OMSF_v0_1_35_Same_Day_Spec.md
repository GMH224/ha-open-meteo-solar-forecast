# Open-Meteo Solar Forecast (GMH224 fork) — Same-day correction (0.1.35)

Status: **confirmed by the owner on 19 Oct 2026** ("build 0.1.35.0"); implemented in 0.1.35.0.
Deviations from the confirmed draft are listed in §8.

## 1. Why

The owner's battery automation reads the forecast's 15-minute `watts` bands of today:

- **2026 version:** once, at sunrise + 1 h.
- **2027 version:** at sunrise (Mar–Oct, rough plan), then at every forecast update from sunrise + 1 h until the
  planned full-charge start (checkpoints); Nov–Feb once at sunrise + 1 h.

An over-optimistic forecast is the expensive error: the battery charge starts too late or is sized too small and the
battery does not fill. 19 Oct 2026 showed the case: a closed cloud deck all day, forecast ≈ 20 kWh, actual ≈ 6.5 kWh,
the miss visible from the first production hour onwards.

The learning hub of 0.1.34 cannot help on such a day: it corrects **systematic** errors of the morning forecast and
never looks at what is happening today.

## 2. Findings that shaped the design (owner data, 9–19 Oct 2026)

| Finding | Consequence |
|---|---|
| 10K inverter stays on-grid around the clock (battery); 5K (East) wakes at sunrise and waits a fixed 30 min ("detecting irradiation") before going on-grid; off at sunset | no start-up threshold in the model (loss < 0.1 kWh/day); first East data at sunrise + 30 min |
| East output 08:00–09:00: clear 1 352 W, partly cloudy 393 W, grey 37–90 W; station at the same time 6–17 W/m² either way | **house production is the early signal**, the station (horizontal sensor, sun very low) is not |
| On 15 and 16 Oct the house was much darker than the station in the morning (local cloud) | the house signal is also the more representative one |
| On fully overcast days East delivers 0.7 × the 10K (= the kWp ratio) | East works normally in grey light; no low-light correction needed |
| Inverter datasheet: start-up 200 V, MPPT 140–980 V; the strings are far above at very low light | start-up is the inverter's internal logic, measured not modelled |

## 3. The model

### 3.1 Quantity and error model

**Quantity.** The clear-sky index of global horizontal light, k = GHI / GHI_clear (Haurwitz clear sky, hour means
over the same sample instants as the light synthesis). It removes the daily and seasonal course of the sun, so that
what remains is the weather. Forecast: k_fc(h) from the served light of hour h. Truth: k(t). Error:
e(t) = k(t) − k_fc(t). Both are clamped to [0, 1.05] (cloud enhancement over 15 minutes stays small; the clamp also
absorbs Haurwitz' known small underestimate of very clear skies on both sides alike).

**Error model.** The error persists within a day and fades: e(t+Δ) = β(Δ)·ê(t) + ε, where ê(t) is the error measured
over the last hour. The best linear prediction (least mean square error) of the future error is β(Δ)·ê(t). If the
error were a first-order autoregressive process with half-life τ and measured without noise, β(Δ) = 0.5^(Δ/τ); ê is
noisy, so the optimal β is smaller than the autocorrelation, ρ(Δ)·σ²/(σ² + σ_n²). Estimating β by least squares (3.3)
includes that shrinkage automatically.

**Corrected index:** k̂(h) = clamp(k_fc(h) + β(Δ_h)·ê, 0, 1.05) for today's remaining hours, Δ_h = lead of the
hour's midpoint. Past hours and other days are never touched.

### 3.2 Measuring ê at the house (response-curve inversion)

Production is plane-of-array light on three orientations; its clear-sky index is not the horizontal one (under a grey
sky East gets ≈ 0.88 × GHI, under a clear morning sky several times GHI). A plain ratio would mix weather and
geometry. Instead:

- **Response curves.** Every hour the hub runs each member's own model (same settings, shading, horizon, forecast
  temperatures) on clear-sky-shaped light at 13 values of k (0 … 1.05, denser at low k; diffuse split by Erbs,
  direct normal derived as for the stage-2 model). Summed per inverter group this gives, for every 15-minute band b,
  a monotone curve E_g,b(k) in kWh.
- **Measured energy.** Per group and band from the energy counter's state changes (step function: the reading at or
  before each band edge), valid only if the counter was reported at or after the band end, the step is not negative
  and not above 1.2 × kWp × duration (+ one resolution step). A band whose counter has not reported 30 min after its
  end is stored as unknown.
- **Window.** The last four completed bands, none before sunrise + 30 min, at least two.
- **Inversion.** k_meas = the k at which Σ_g,b E_g,b(k) equals the measured energy of the window (linear
  interpolation on the monotone curve; at or above the curve's top: k = 1.05, flagged *saturated*).
- **Forecast index of the window.** k_fc,win = Σ w_g,b·k_fc,g(h(b)) / Σ w_g,b with w = E_g,b(1) (the clear-sky
  energy): the same weighting the measurement carries. Each group uses its own served light (stage 2 makes the groups
  differ slightly).
- **ê = k_meas − k_fc,win.** One number for all groups: the weather is one; the East array, which sees the morning
  best, automatically carries the most weight.

**Validity (derived, not guessed).** The counters resolve 0.01 kWh; with E_clear = Σ E_g,b(1) ≥ 0.3 kWh in the
window this moves ê by at most ±0.033 per counter (±0.067 worst case for two). Smaller windows are refused, as are a
missing or stale counter and an implausible step.

### 3.3 Learning β(Δ) and σ(Δ) from the owner's days

- Every **usable day** (≥ 80 % of the bands with E_clear ≥ 0.05 kWh measured, at least one decision) yields pairs
  (ê at a decision time, e at a later band) for decision times at sunrise + 1 h and then every :00/:30 up to 14:00.
  e(b) = k_meas(b) − k_fc,d(h(b)), with k_fc,d the forecast index recorded at that decision. Weight: E_clear(b).
- β per lead bucket (0–1, 1–2, 2–4, 4–8, 8–16 h) by weighted least squares through the origin, **shrunk toward the
  prior** 0.5^(Δ_mid/3 h) with a prior weight of 5 days: β = (n·β_data + 5·β_prior) / (n + 5), n = days contributing;
  clamped to [0, 1]. Without data: the prior.
- σ per bucket: weighted root mean square of the residuals e − β·ê; σ_u: the unconditional spread of e (used when no
  valid signal exists).
- Two situations, estimated separately: **improving** (the forecast index rises by ≥ 0.15 within the next 3 h — the
  "deck will clear" case of 19 Oct) and **other**.
- No look-ahead: the table is recomputed nightly from usable days strictly before today (last 60 usable days).

**Simulation check (synthetic AR(1) errors, σ = 0.25, measurement noise 0.08, β learned from 60 days, tested on
2 000):**

| True half-life | Lead | Raw error | Fixed 3 h fade | Learned β |
|---|---|---|---|---|
| 1 h (showers) | 2 h | 0.247 | 0.264 (**worse than raw**) | 0.248 |
| 3 h | 1 h | 0.249 | 0.167 | 0.166 |
| 9 h (closed deck) | 4 h | 0.248 | 0.190 | 0.180 |

The learned β matches the theory (e.g. 0.74 vs 0.72), is never worse than the raw forecast beyond sampling noise, and
beats a fixed fade where persistence is long. A fixed fade can be worse than doing nothing on showery days, which is
why SDC has its own check before it acts. The same check runs in the test suite through the integration's own
learning code (`test_learned_persistence_matches_the_error_model`).

### 3.4 Applying the correction

k̂ is converted back to a light shift per hour of today: Δkt = (k̂ − k_fc)·kt_clear (|Δkt| ≤ 0.6), and the diffuse
fraction moves along Erbs (Δkd = Erbs(kt + Δkt) − Erbs(kt): grey → more diffuse, so the East/South/West split stays
physical). It goes through the same function as the learning correction, so watts, Wh, day totals, 15-minute values
and clipping stay consistent. SDC acts on top of the forecast that would be served without it (raw, or
learning-corrected in state `applied`).

### 3.5 Cautious bands (`watts_cautious`)

- k_c(h) = clamp(k̂(h) − 0.8416·σ(Δ_h), 0, k̂(h)); 0.8416 is the 80 % one-sided normal quantile (20th percentile).
  Without a valid signal: σ_u.
- Bands from k_c through the same model; attribute `watts_cautious` of today's energy sensor, same format as `watts`,
  each value capped at the served `watts`.
- Summing per-band 20th percentiles is exact when the errors of a day are fully correlated and more cautious
  otherwise; intraday errors are strongly correlated, so the day total is close to its 20th percentile and on the
  safe side.
- Published only after 10 usable days. **Checked, not assumed:** the share of (day, decision time, group) where the
  measured remaining energy fell below the cautious one is shown as *Cautious coverage* (target about 20 %).

### 3.6 Decision-time evaluation

- **Stage 1 (weather learning):** the skill check judges the *decision forecast* — the first hourly run at/after
  sunrise + 1 h (store key `issue6` kept; export `forecast_decision.csv`) — instead of 06:00. Energy half: each
  member's first refresh at/after sunrise + 1 h. Rules unchanged (≥ 14 days, ≥ 5 % better, energy not worse).
  Algorithm version 3: the count restarts; the 06:00 records are dropped by the store migration (not comparable).
- **SDC check (nightly, last 28 days):** at the first decision (sunrise + 1 h) and the first at/after 10:00 and 12:00:
  absolute error of each group's remaining energy, without vs with SDC (both as the members computed them at that
  moment). Passes with **≥ 7 usable days, mean error ≥ 5 % lower and lower on at least 5 of the last 7 days**. Served
  only with the check passed **and** the switch *Same-day correction* on. Until then: shadow (computed, recorded,
  shown, not served).

## 4. Run schedule

| When | What |
|---|---|
| every 5 min (hh:m5:40) after sunrise | response curves (rebuilt hourly), bands finalised, ê measured, decision recorded at its time (not later than 15 min) |
| every member refresh (10 min) | corrected and cautious light from the latest ê (not older than 15 min), estimates, shadow totals, report to the hub |
| 03:30 nightly | β/σ table, SDC check, purge (same-day records kept 120 days) |
| startup | counter readings of the last 30 h from the recorder |

## 5. What is not changed

- Day exclusion rule (station not representative): the data of 15/16 Oct showed it worked as intended.
- No inverter start-up threshold.
- Stage 1, stage 2, memory and export unchanged apart from the additions above.
- Entries outside a hub group and Open-Meteo mode: untouched.

## 6. Transparency

| Where | What |
|---|---|
| sensor *Same-day correction* (state: ê) | state (collecting/shadow/eligible/applied/paused), switch, served, signal (k_meas, k_fc, window, clear and measured energy, saturated, situation, reason), β and σ table, prior, usable days, last run |
| sensor *Same-day improvement* (%) | the check: errors without/with, days, better of last 7, reasons |
| sensor *Cautious coverage* (%) | checked, below, published, usable days |
| switch *Same-day correction* | owner authority |
| member `learning` attribute | `same_day`: valid, ê, situation, served, today without/with, cautious published |
| `next_hours` | also total cloud and estimated cell temperature |
| diagnostics | signal, last run, table, check and history, today's decisions, counter readings |
| export | `same_day_decisions.csv` (per decision: ê, situation, remaining energy without/with/cautious/measured per group, forecast index per hour), `same_day_bands.csv` (per band: k_meas, clear-sky energy, measured energy per group) |
| events | owner switch, check passed/failed |

## 7. Safety (ICS)

- SDC never raises into the forecast path; any fault leaves the forecast as it would be without SDC, a failed run
  clears the signal, a signal older than 15 min is not used.
- Bounded: |Δkt| ≤ 0.6, k ≤ 1.05, β in [0, 1], today only, future hours only.
- Owner authority: its own switch; served only after its own check.
- Untrusted input: counter units checked (Wh/kWh/MWh), negative or non-finite readings refused, steps bounded,
  freshness required. Stored section validated strictly (schema 3); a malformed store is quarantined as before.
- Read-only towards devices; no new network access.
- Replay tool recomputes the stored β/σ table from the export with an independent implementation.

## 8. Implementation decisions and deviations from the confirmed draft

| # | Draft | Implemented | Why |
|---|---|---|---|
| 1 | ê found by bisection, model run on the forecast light shifted by ê | inversion of hourly response curves (model on clear-sky-shaped light at 13 k values) | same physics, no model runs per refresh, and k_meas becomes forecast-independent, so the learning pairs (ê, e) live in exactly one definition of k |
| 2 | counter readings interpolated linearly at band edges | step function (last reading at or before the edge) | a counter changes in 0.01 kWh steps; interpolation would invent energy between readings |
| 3 | correction per 15-minute band | per hour of light (the synthesis' unit), output in 15-minute bands | light records are hourly; the library interpolates the bands |
| 4 | lead buckets up to 8 h | also 8–16 h | summer days: decisions at 06:30 reach sunset at 21:30 |
| 5 | stage 1 judged at sunrise + 1 h, 10:00 and 12:00 | stage 1 at sunrise + 1 h (its decision forecast); SDC at all three | stage 1 corrects the morning forecast; the later times are the same-day correction's domain |
| 6 | export `same_day.csv` | `same_day_decisions.csv` and `same_day_bands.csv` | decisions and bands have different keys |
| 7 | replay recomputes every SDC decision | replay recomputes the β/σ table | ê depends on the PV model (response curves), which the independent tool deliberately does not contain |
| 8 | — | same-day records kept 120 days | the table uses the last 60 usable days, the check 28 days; the store stays small |
| 9 | — | the 5 % rule is evaluated with a 1e-9 tolerance | exactly 5 % must pass despite float rounding |
