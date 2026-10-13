# Open-Meteo Solar Forecast (GMH224 fork) v0.1.34.1 — Release Audit (ICS)

**Release type:** fix — learning hub could not form station hours on the
live system.
**Basis:** live diagnostics of 13 Oct 2026 (Home Assistant 2026.10.1,
Fusion 0.3.4); request to Fusion `FUSION_station_recent_values_request.md`;
Fusion's reply `SOLAR_TEAM_v0.3.5.md` (format confirmed, shipped in 0.3.5).
**Owner decision (13 Oct 2026):** fix at the source (Fusion keeps every
record) rather than relaxing the 5-of-6 completeness rule.
**Invariant:** with Fusion < 0.3.5 behaviour identical to 0.1.34; nothing
outside the learning hub changed.

## 1. Finding

First live run of 0.1.34: forecast archive, production backfill (6 379 /
6 358 hours), member reports and nightly run all worked; but after three
hours `station_hours` was still 0. Between 04:40 and 07:40 UTC the station
sensor showed 10 of 19 ten-minute records. Fusion shows only the latest
record; MeteoSwiss publishes with a varying 10–20 minute delay, so a poll
can see the same record twice and miss the next. With ≥ 5 of 6 required per
hour, no hour completes and the learner can never start. Not a fault in the
forecast path (unchanged); a gap between the two integrations' assumptions
that only the live run could show.

## 2. Changes

| Area | Change | Files |
|---|---|---|
| Engine | `parse_station_list`: validates `recent_values` record by record exactly like the latest value (ranges, grid, nothing filled); list > 100 entries rejected as a whole; records outside [reference − 7 h, reference + 15 min] rejected; merge rule: a record is replaced only by one at least as complete | `learning.py` |
| Hub | latest record and list merged by one function, used for live updates, the hourly run and the recorder backfill (list judged against the state's own time); `station` status shows the list count and rejects | `learning_hub.py` |
| Fixes | `since` set on a fresh store; startup report kept separately (`last_run.startup`, diagnostics `last_startup`) | `learning_hub.py`, `learning_entities.py`, `diagnostics.py` |
| Version | 0.1.34.1 | `manifest.json` |

## 3. Safety analysis (ICS)

| Concern | Measure |
|---|---|
| Untrusted list | type check, size bound (100), per-record validation and time window; rejects counted and shown |
| Data integrity | no interpolation or carrying forward; a withdrawn value never erases a stored record; a less complete record never replaces a more complete one |
| Completeness rule | unchanged (≥ 5 of 6); the fix supplies the data instead of lowering the bar |
| Backward compatibility | without the attribute the code path is the 0.1.34 one (`test_without_recent_values_the_strict_rule_leaves_hours_open`) |
| Recorder | the list is not recorded by Fusion; the backfill uses it where present (fake in the test records it; on the live system Fusion excludes it, so a restart is refilled by the next live update, up to 6 h back) |

## 4. Verification

540 tests passing (+152 subtests); pyflakes clean; mypy without new
findings (38 in pre-0.1.34 modules, unchanged). New mutations M191–M198:
8/8 caught. Learning mutations M109–M190 re-run on the changed code:
80/82 + 2 documented equivalent (unchanged from 0.1.34). M01–M108 target modules not changed by this release; their
0.1.34 results stand.

## 5. Defects in this pass's own work

| ID | Defect | How found | Fix |
|---|---|---|---|
| D-1 | The startup backfill read only the latest record of each historical state, not its list | `test_backfill_reads_recent_values_relative_to_the_state_time` (written for the release) | one merge function for live and history |
| D-2 | First version of the skip test never delivered the last record | the test | test corrected |
| P-1 | 0.1.34 assumed every 10-minute record reaches the sensor state; never true on a live system | live diagnostics | this release; spec §17 row 15 |

## 6. Owner actions

1. Install SwissWeather Fusion 0.3.5 (if not done).
2. Upload 0.1.34.1 (zip), release `v0.1.34.1`, HACS redownload, restart.
3. After about an hour: diagnostics `inputs.station.recent_values` ≈ 36,
   `counts.station_hours` > 0 and growing by one per hour.
