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
INIT_PY = PKG + "__init__.py"
SHD = PKG + "shading.py"
SHP = PKG + "shading_provider.py"
FLOW = PKG + "config_flow.py"

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
     "HOURS_PER_LOCAL_DAY = 24", "HOURS_PER_LOCAL_DAY = 1"),
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
    # --- 0.1.33.4: external-audit remediation ----------------------------
    ("M51", "V-1: partial local days dropped in hybrid", PKG + "hybrid.py",
     "    local_selected = set(local_days) | partial_local_days", "    local_selected = set(local_days)"),
    ("M52", "OMSF-004: empty forecast accepted", COORD,
     "        if not estimate.watts or not estimate.wh_period:\n            raise", "        if False:\n            raise"),
    ("M53", "OMSF-007: time window not applied", SRC,
     "    if now is None:\n        return False\n    return not", "    return False\n    return not"),
    ("M54", "OMSF-008: no size cap", SRC,
     "MAX_SERIES_ENTRIES = 500", "MAX_SERIES_ENTRIES = 10**9"),
    ("M55", "OMSF-011: 23 hours count as complete", PKG + "hybrid.py",
     "HOURS_PER_LOCAL_DAY = 24", "HOURS_PER_LOCAL_DAY = 23"),
    ("M56", "OMSF-018: URL query strings not removed", PKG + "errors.py",
     '    text = _URL_QUERY.sub(r"\\1?<redacted>", text)\n', ""),
    ("M57", "OMSF-018: error length not bounded", PKG + "errors.py",
     "MAX_ERROR_LENGTH = 200", "MAX_ERROR_LENGTH = 10**9"),
    ("M58", "OMSF-013: no retry after a failed history read", PROV,
     "            if self._load_attempts >= HISTORY_LOAD_ATTEMPTS:", "            if True:"),
    ("M59", "OMSF-015: malformed supported_features raises", PROV,
     "        except (TypeError, ValueError):\n            return \"weather_no_hourly\"",
     "        except (KeyError,):\n            return \"weather_no_hourly\""),
    ("M60", "OMSF-016: non-finite geometry accepted at parse", SRC,
     "    if not math.isfinite(parsed):  # OMSF-016", "    if False:  # OMSF-016"),
    ("M61", "OMSF-005: snow depth not held", PROV,
     "SNOW_HOLD = timedelta(hours=24)", "SNOW_HOLD = timedelta(0)"),
    ("M62", "OMSF-006: temperature quality always 'forecast'", PROV,
     '"temperature": "forecast" if fresh_temps else "current_value_fallback",',
     '"temperature": "forecast",'),
    ("M63", "OMSF-001: several entries -> silently the last one", INIT_PY,
     "        else:\n            raise ServiceValidationError(\n                f\"{len(loaded)}",
     "        else:\n            entry = loaded[-1]\n        if False:\n            raise ServiceValidationError(\n                f\"{len(loaded)}"),
    ("M64", "OMSF-002: service without schema", INIT_PY,
     "        schema=SERVICE_SCHEMA,\n", ""),
    ("M65", "OMSF-002: bool accepted as coordinate", INIT_PY,
     "        if isinstance(value, bool) or not isinstance(value, (int, float)):",
     "        if not isinstance(value, (int, float)):"),
    ("M66", "OMSF-003: no retention limit", PKG + "const.py",
     "RETAINED_MAX_AGE_HOURS = 6", "RETAINED_MAX_AGE_HOURS = 100000"),
    ("M67", "OMSF-003: source sensor unavailable with the others", PKG + "sensor.py",
     "        forecast sensors are unavailable (0.1.33.4, OMSF-003).\"\"\"\n        return True",
     "        forecast sensors are unavailable (0.1.33.4, OMSF-003).\"\"\"\n        return self.coordinator.last_update_success"),
    ("M68", "OMSF-009: non-finite horizon values accepted", COORD,
     "    if not numpy.all(numpy.isfinite(horizon_data)):", "    if False:"),
    ("M69", "OMSF-009: horizon elevation range not checked", COORD,
     "    if numpy.any(elevations < -90) or numpy.any(elevations > 90):", "    if False:"),
    ("M70", "OMSF-009: horizon file size not capped", COORD,
     "HORIZON_FILE_MAX_BYTES = 64 * 1024", "HORIZON_FILE_MAX_BYTES = 10**9"),
    ("M71", "OMSF-012: AttributeError not caught for retained data", COORD,
     "        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):",
     "        except (KeyError, TypeError, ValueError, OverflowError):"),
    ("M72", "OMSF-012: retained values not checked for finiteness", COORD,
     '    if value != value or value in (float("inf"), float("-inf")):\n        raise',
     '    if False:\n        raise'),
    ("M73", "OMSF-019: 15-minute series recorded", PKG + "recorder.py",
     "    return {ATTR_WATTS, ATTR_WH_PERIOD, ATTR_WH_PERIOD_15M}", "    return {ATTR_WATTS, ATTR_WH_PERIOD}"),
    # ---- 0.1.33.5: partial shading -------------------------------------
    ("M74", "blocked share inverted (k instead of 1-k), averages", SHD,
     "blocked = dni * np.where(up, caoi * (1 - k), 0).mean(axis=1)",
     "blocked = dni * np.where(up, caoi * k, 0).mean(axis=1)"),
    ("M75", "blocked share inverted, instants", SHD,
     "blocked_i = dni_i * caoi_i * (1 - k_i)", "blocked_i = dni_i * caoi_i * k_i"),
    ("M76", "low-sun guard removed", SHD,
     "    up = el > MIN_SUN_ELEVATION_DEG\n", "    up = el > -90.0\n"),
    ("M77", "DNI not capped", SHD,
     "    dni = np.minimum(dni, MAX_DNI_W_M2)\n", "    dni = dni\n"),
    ("M78", "diffuse light reduced as well", SHD,
     "new_gti = np.where(ok, np.clip(gti - blocked, 0, None), gti)",
     "new_gti = np.where(ok, np.clip(gti * k.mean(axis=1), 0, None), gti)"),
    ("M79", "API azimuth used as compass bearing", SHP,
     'azimuth = (float(params["azimuth"]) + 180.0) % 360.0',
     'azimuth = float(params["azimuth"]) % 360.0'),
    ("M80", "array counter not reset per estimate", SHP,
     "        self._shading_index = 0\n        self.shading_stats = []",
     "        self.shading_stats = []"),
    ("M81", "Open-Meteo mode never shaded", COORD,
     "ShadedOpenMeteoSolarForecast if active_shading else OpenMeteoSolarForecast",
     "OpenMeteoSolarForecast if active_shading else OpenMeteoSolarForecast"),
    ("M82", "parity broken: shaded class without shading", COORD,
     "ShadedOpenMeteoSolarForecast if active_shading else OpenMeteoSolarForecast",
     "ShadedOpenMeteoSolarForecast if True else OpenMeteoSolarForecast"),
    ("M83", "local forecast not given the tables", COORD,
     "                self.local_forecast.shading_tables = active_shading\n",
     "                pass\n"),
    ("M84", "bad shading file fails the setup", SHP,
     "        except Exception as err:  # noqa: BLE001 - a bad file must not stop the forecast\n",
     "        except ZeroDivisionError as err:\n"),
    ("M85", "tracking check removed", SHP,
     '            if tracking[index] != "none":\n', "            if False:\n"),
    ("M86", "repair issue not cleared after a fix", SHP,
     "        ir.async_delete_issue(hass, DOMAIN, issue)\n        status = ShadingStatus(",
     "        status = ShadingStatus("),
    ("M87", "cache ignores file changes", SHP,
     "if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:",
     "if cached is not None:"),
    ("M88", "half-cut treated as full-cell", SHD,
     "        section = darkest.mean(axis=-1)\n", "        section = darkest.min(axis=-1)\n"),
    ("M89", "string MPP replaced by the weakest section", SHD,
     "power += (caps * (n - np.arange(n))[None, :]).max(axis=1)",
     "power += caps[:, 0] * n"),
    ("M90", "section limited by mean instead of darkest cell", SHD,
     "    darkest = light.min(axis=-1)  # (nd, modules, diodes, 2)",
     "    darkest = light.mean(axis=-1)  # (nd, modules, diodes, 2)"),
    ("M91", "transparency ignored", SHD,
     "light[m] = np.where(hit, light[m] * tr[m], light[m])",
     "light[m] = np.where(hit, 0.0, light[m])"),
    ("M92", "monthly transparency not detected", SHD,
     "monthly = any(np.ptp(o.transparency) > 0 for o in geom.obstacles)", "monthly = False"),
    ("M93", "full-circle table does not wrap", SHD,
     "            wraps = True\n", "            wraps = False\n"),
    ("M94", "outside the table range treated as shaded", SHD,
     "return np.where(inside_az & ~above, np.clip(f, 0.0, 1.0), 1.0)",
     "return np.clip(f, 0.0, 1.0)"),
    ("M95", "duplicate table rows accepted", SHD,
     "            if seen[ai[idx], ei[idx]]:\n", "            if False:\n"),
    ("M96", "factor range not checked", SHD,
     "    if np.any((fac < 0) | (fac > 1)):\n", "    if False:\n"),
    ("M97", "unknown YAML keys accepted", SHD,
     "    unknown = sorted(str(k) for k in obj if k not in allowed)", "    unknown = []"),
    ("M98", "polygon planarity not checked", SHD,
     "        if np.max(np.abs((vertices - centre) @ normal)) > 0.05:\n", "        if False:\n"),
    ("M99", "obstacle overlapping the modules accepted", SHD,
     "        if obstacle.contains(corners).any():\n", "        if False:\n"),
    ("M100", "work budget ignored", SHD,
     "        if n_dir * n_points * primitive_cost <= WORK_BUDGET:\n", "        if True:\n"),
    ("M101", "gable roof flattened to ridge height", SHD,
     "    rise = (ridge - eave) / half  # roof slope (m per m)", "    rise = 0.0"),
    ("M102", "cylinder/cone radius ignored", SHD,
     "            hit |= ok & (f <= 0) & (rad >= 0)", "            hit |= ok & (rad >= 0)"),
    ("M103", "config flow does not validate the file", FLOW,
     "    message = await hass.async_add_executor_job(validate_shading_file, path)\n",
     "    message = None\n"),
    ("M104", "sun azimuth mirrored (morning/afternoon swapped)", SHD,
     "    az = np.where(ha > 0, np.mod(az + 180, 360), np.mod(540 - az, 360))",
     "    az = np.where(ha < 0, np.mod(az + 180, 360), np.mod(540 - az, 360))"),
    ("M105", "averages evaluated at the label instead of inside the quarter", SHD,
     "    offsets = np.array([-12.5, -7.5, -2.5]) * 60.0", "    offsets = np.array([0.0, 0.0, 0.0]) * 60.0"),
    ("M106", "month index off by one", SHD,
     '.astype("datetime64[M]").astype(int) % 12 + 1', '.astype("datetime64[M]").astype(int) % 12'),
    ("M107", "string membership ignored", SHD,
     "section[:, members, :].reshape(section.shape[0], -1)", "section.reshape(section.shape[0], -1)"),
    ("M108", "unshaded not snapped to exactly 1 (parity noise)", SHD,
     "    k = np.where(k > 1 - 1e-9, 1.0, k)  # unshaded must be exactly unshaded\n", ""),
]


def run(root: Path, only: str | None, start: str | None = None) -> list[dict]:
    results = []
    python = sys.executable
    for mid, desc, rel, original, mutated in MUTATIONS:
        if only and mid != only:
            continue
        if start and int(mid[1:]) < int(start[1:]):
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
            # Shading tests first (0.1.33.5): a caught mutation stops early;
            # pytest de-duplicates the files listed again via "tests".
            first = sorted(str(p.relative_to(work)) for p in (work / "tests").glob("test_shading_*.py"))
            proc = subprocess.run(
                [python, "-m", "pytest", "-q", "-x", "-W", "ignore", "-p", "no:randomly", *first, "tests"],
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
    parser.add_argument("--from", dest="start", help="run mutations from this id on (e.g. M74)")
    parser.add_argument("--out", default="mutation_results.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    results = run(root, args.only, args.start)
    Path(args.out).write_text(json.dumps(results, indent=2))
    escaped = [r for r in results if r["status"] != "CAUGHT"]
    print(f"{len(results) - len(escaped)}/{len(results)} caught")
    return 1 if escaped else 0


if __name__ == "__main__":
    raise SystemExit(main())
