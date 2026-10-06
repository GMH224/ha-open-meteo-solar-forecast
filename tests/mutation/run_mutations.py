"""Mutation record for 0.1.33.2 — "if I break it, does a test fail?"

Each mutation is a plausible implementation mistake, applied textually to a
pristine copy of the repository, followed by a full test run. A mutation is
*caught* if at least one test fails. Results are written as JSON and
transcribed into omsf_v0_1_33_2_ICS_quality_bug_testing_report.md.

Usage (from the repository root):
    python tests/mutation/run_mutations.py [--only M07] [--out mutation_results.json]

The working tree is never modified: every mutation runs in a temporary copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = "custom_components/open_meteo_solar_forecast/"
SRC = PKG + "local_source.py"
PROV = PKG + "local_provider.py"
COORD = PKG + "coordinator.py"

# (id, description, file, original, mutated)
MUTATIONS = [
    ("M01", "average labelled at the quarter START instead of its end", SRC,
     "        quarter_start = label - QUARTER\n        hour_start = floor_hour(quarter_start)",
     "        quarter_start = label - QUARTER\n        hour_start = floor_hour(label)"),
    ("M02", "first label gets an average (covers the hour before the data)", SRC,
     "kt_kd = hour_kt.get(hour_start) if label > first_hour else None",
     "kt_kd = hour_kt.get(hour_start) or hour_kt.get(first_hour)"),
    ("M03", "clearness index not clamped", SRC,
     "CLEARNESS_INDEX_MAX = 1.0", "CLEARNESS_INDEX_MAX = 99.0"),
    ("M04", "D-01 regression: hour clearness on a finer grid than the quarters", SRC,
     "    moments = _hour_sample_moments(record.start)",
     "    moments = [record.start + timedelta(minutes=k + 0.5) for k in range(60)]"),
    ("M05", "D-02 regression: low-sun beam not reassigned to diffuse", SRC,
     "    if sun.cos_zenith < COS_ZENITH_FLOOR:\n        # Sun within",
     "    if False:\n        # Sun within"),
    ("M06", "anisotropy index not clamped to 1", SRC,
     "anisotropy = min(dni / sun.extraterrestrial_normal, 1.0)",
     "anisotropy = dni / sun.extraterrestrial_normal"),
    ("M07", "azimuth convention: Open-Meteo value used as compass bearing", SRC,
     "azimuth = math.radians((azimuth_om_deg + 180.0) % 360.0)",
     "azimuth = math.radians(azimuth_om_deg % 360.0)"),
    ("M08", "ground-reflected term dropped", SRC,
     "ground = ghi * albedo * (1 - math.cos(tilt)) / 2", "ground = 0.0"),
    ("M09", "published instants ignored (hour-average anchor only)", SRC,
     "    if record is not None and record.has_instant:", "    if False:"),
    ("M10", "diffuse-exceeds-global check removed", SRC,
     "    if dhi > ghi + DIFFUSE_TOLERANCE_W_M2:", "    if False:"),
    ("M11", "incomplete triple accepted", SRC,
     "    if any(v is None for v in values):\n        return False, \"incomplete GHI/DNI/DHI triple\"",
     "    if all(v is None for v in values[:1]):\n        return False, \"incomplete GHI/DNI/DHI triple\""),
    ("M12", "booleans accepted as numbers", SRC,
     "    if value is None or isinstance(value, bool):", "    if value is None:"),
    ("M13", "non-hour-aligned period_start accepted", SRC,
     "    if start != floor_hour(start):\n            report.reject(index, \"period_start not on a full hour\")\n            continue",
     "    if False:\n            continue"),
    ("M14", "stale-series check removed", SRC,
     "    if first < current_hour - HOUR:", "    if False:"),
    ("M15", "current-hour requirement removed", SRC,
     "    if current is None or not current.has_average:", "    if False:"),
    ("M16", "history resurrects future hours from cache", SRC,
     "        if keep_from <= start < current_hour", "        if keep_from <= start"),
    ("M17", "history not pruned", SRC,
     "        if start >= keep_from:", "        if True:"),
    ("M18", "Fahrenheit not converted", SRC,
     "        if unit_norm == \"F\":", "        if unit_norm == \"X\":"),
    ("M19", "temperature gaps bridged without limit", SRC,
     "    if t1 - t0 > timedelta(hours=3):", "    if False:"),
    ("M20", "snow depth not applied", SRC,
     "        snow.append(snapshot.snow_depth_m)", "        snow.append(0.0)"),
    ("M21", "polar day/night can yield sunset == sunrise", SRC,
     "        return best, best + timedelta(seconds=1)", "        return best, best"),
    ("M22", "fallback to Open-Meteo on by default", COORD,
     "entry.options.get(CONF_LOCAL_FALLBACK_OPEN_METEO, False)",
     "entry.options.get(CONF_LOCAL_FALLBACK_OPEN_METEO, True)"),
    ("M23", "fallback taken although disabled", COORD,
     "            if not self.fallback_to_open_meteo:\n                raise LocalDataError(message) from err",
     "            if False:\n                raise LocalDataError(message) from err"),
    ("M24", "local refresh interval 30 min", COORD,
     "            update_interval = timedelta(minutes=LOCAL_UPDATE_MINUTES)",
     "            update_interval = timedelta(minutes=OPEN_METEO_UPDATE_MINUTES)"),
    ("M25", "Open-Meteo refresh interval changed", COORD,
     "            update_interval = timedelta(minutes=OPEN_METEO_UPDATE_MINUTES)",
     "            update_interval = timedelta(minutes=LOCAL_UPDATE_MINUTES)"),
    ("M26", "retained-source state not reported", COORD,
     "            self.active_source = ACTIVE_SOURCE_RETAINED\n            return retained\n\n        if self.uses_local",
     "            return retained\n\n        if self.uses_local"),
    ("M27", "history never persisted", PROV,
     "        self._store.async_delay_save(lambda: data, 30)", "        pass"),
    ("M28", "Fusion companion suffix wrong", PKG + "const.py",
     'FUSION_IRRADIANCE_SUFFIX = "_solar_ghi"', 'FUSION_IRRADIANCE_SUFFIX = "_solar_dni"'),
    ("M29", "missing temperature accepted (no current temperature either)", PROV,
     "        if not fresh_temps and fallback_temp is None:", "        if False:"),
    ("M30", "history merge skipped (only the fresh series is used)", PROV,
     "        self._hours = merge_history(self._hours, fresh_hours, now, keep_from)",
     "        self._hours = dict(fresh_hours)"),
    ("M31", "D-05 regression: day sensors not masked in local mode", PKG + "sensor.py",
     "            if target is not None and target not in covered:\n                return None",
     "            if False:\n                return None"),
    ("M32", "night hours treated as daylight in day coverage", SRC,
     "                _elevation_deg(hour_start + QUARTER * q + QUARTER / 2, latitude, longitude) > -0.833",
     "                _elevation_deg(hour_start + QUARTER * q + QUARTER / 2, latitude, longitude) > -90"),
    ("M33", "fallback keeps the local coverage mask on Open-Meteo data", COORD,
     "            self._set_day_sources(None)  # Open-Meteo data: no masking",
     "            pass"),
    ("M34", "day coverage not persisted with the retained forecast", COORD,
     "        if self.local_complete_dates is not None:\n            data[\"local_complete_dates\"]",
     "        if False:\n            data[\"local_complete_dates\"]"),
    ("M35", "D-06 regression: weather service call unbounded", PROV,
     "            async with asyncio.timeout(SERVICE_TIMEOUT_SECONDS):",
     "            async with asyncio.timeout(None):"),
    ("M36", "D-07 regression: missing stored coverage masks every day", COORD,
     "                else:\n                    self._set_day_sources(None)\n",
     "                else:\n                    self._set_day_sources({})\n"),
    ("M37", "parity: Open-Meteo failure message changed", COORD,
     'raise UpdateFailed(f"Error communicating with API: {err}") from err',
     'raise UpdateFailed(f"Error fetching forecast: {err}") from err'),
    # --- 0.1.33.3: hybrid mode and release process -----------------------
    ("M38", "hybrid: Open-Meteo overrides a complete local day", PKG + "hybrid.py",
     "om_days = complete_open_meteo_days(open_meteo, tz) - set(local_days)",
     "om_days = complete_open_meteo_days(open_meteo, tz)"),
    ("M39", "hybrid: incomplete Open-Meteo days accepted", PKG + "hybrid.py",
     "MIN_HOURS_FOR_COMPLETE_DAY = 23", "MIN_HOURS_FOR_COMPLETE_DAY = 1"),
    ("M40", "hybrid: days taken in the source's own timezone", PKG + "hybrid.py",
     "        if _day(moment, tz) in days", "        if moment.date() in days"),
    ("M41", "hybrid: Open-Meteo fetched every local cycle", PKG + "const.py",
     "HYBRID_OPEN_METEO_REFRESH_MINUTES = 30", "HYBRID_OPEN_METEO_REFRESH_MINUTES = 0"),
    ("M42", "hybrid: no cache after a failed refresh", PKG + "const.py",
     "HYBRID_OPEN_METEO_MAX_AGE_MINUTES = 180", "HYBRID_OPEN_METEO_MAX_AGE_MINUTES = 0"),
    ("M43", "hybrid: cache used without age limit", PKG + "const.py",
     "HYBRID_OPEN_METEO_MAX_AGE_MINUTES = 180", "HYBRID_OPEN_METEO_MAX_AGE_MINUTES = 100000"),
    ("M44", "hybrid: Open-Meteo failure fails the whole refresh", COORD,
     "        except Exception as err:  # noqa: BLE001 - local days must survive\n",
     "        except Exception as err:  # noqa: BLE001 - local days must survive\n            raise\n"),
    ("M45", "hybrid behaves like local (no Open-Meteo days)", COORD,
     "        if self.weather_source != SOURCE_HYBRID:", "        if True:"),
    ("M46", "hybrid not treated as a local-based source", PKG + "const.py",
     "LOCAL_BASED_SOURCES = (SOURCE_LOCAL, SOURCE_HYBRID)", "LOCAL_BASED_SOURCES = (SOURCE_LOCAL,)"),
    ("M47", "day sources not persisted", COORD,
     "        if self.day_sources is not None:\n            data[\"day_sources\"]",
     "        if False:\n            data[\"day_sources\"]"),
    ("M48", "0.1.33.2 retained coverage not read after upgrade", COORD,
     "                elif raw_dates is not None:  # written by 0.1.33.2",
     "                elif False:  # written by 0.1.33.2"),
    ("M49", "source attribute added in Open-Meteo mode", PKG + "sensor.py",
     "            if day_sources is not None:\n                attributes[\"source\"]",
     "            if True:\n                attributes[\"source\"] = None\n            if day_sources is not None:\n                attributes[\"source\"]"),
    ("M50", "release process: zip_release restored", "hacs.json",
     '"render_readme": true,', '"render_readme": true,\n    "zip_release": true,'),
]


def run(root: Path, only: str | None) -> list[dict]:
    results = []
    python = sys.executable
    for mid, desc, rel, original, mutated in MUTATIONS:
        if only and mid != only:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(root, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            target = work / rel
            text = target.read_text()
            count = text.count(original)
            if count != 1:
                results.append({"id": mid, "description": desc, "status": f"NOT APPLIED (matches={count})"})
                print(mid, "NOT APPLIED", count, flush=True)
                continue
            target.write_text(text.replace(original, mutated))
            proc = subprocess.run(
                [python, "-m", "pytest", "-q", "-x", "-W", "ignore", "-p", "no:randomly"],
                cwd=work, capture_output=True, text=True, timeout=900,
            )
            failed = [
                line.split(" - ")[0].split(" ", 1)[1].strip()
                for line in proc.stdout.splitlines()
                if line.startswith(("FAILED tests/", "ERROR tests/"))
            ]
            status = "CAUGHT" if proc.returncode != 0 else "ESCAPED"
            results.append({"id": mid, "description": desc, "file": rel, "status": status, "first_failure": failed[:1]})
            print(mid, status, failed[:1], flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only")
    parser.add_argument("--out", default="mutation_results.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    results = run(root, args.only)
    Path(args.out).write_text(json.dumps(results, indent=2))
    escaped = [r for r in results if r["status"] != "CAUGHT"]
    print(f"{len(results) - len(escaped)}/{len(results)} caught")
    return 1 if escaped else 0


if __name__ == "__main__":
    raise SystemExit(main())
