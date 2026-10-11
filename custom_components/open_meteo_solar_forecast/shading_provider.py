"""Home Assistant glue for partial shading (0.1.33.5).

* reads shading files (size-bounded, executor), with a process-wide cache
  keyed by path, size, modification time and content digest, so a reload
  does not ray-trace a geometry again;
* reports problems as repair issues and keeps the forecast running without
  shading for that array (a faulty shading file never stops the forecast);
* ``ShadingMixin`` applies the tables to the per-array responses of both the
  Open-Meteo and the local forecast class, right before the library turns
  irradiance into power.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from open_meteo_solar_forecast import OpenMeteoSolarForecast

from .const import DOMAIN
from .errors import sanitize_error
from .shading import (
    SHADING_FILE_MAX_BYTES,
    ShadingError,
    ShadingTable,
    load_shading_text,
    shade_payload,
)

LOGGER = logging.getLogger(__name__)

# path -> (size, mtime_ns, table); bounded so a changing path list cannot grow it
_CACHE: dict[str, tuple[int, int, ShadingTable]] = {}
_CACHE_MAX = 32


@dataclass
class ShadingStatus:
    """What happened with the shading file of one array (diagnostics)."""

    array: int
    path: str | None
    state: str  # "none" | "table" | "geometry" | "error"
    error: str | None = None
    digest: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "array": self.array,
            "file": os.path.basename(self.path) if self.path else None,
            "state": self.state,
            "error": self.error,
            "digest": self.digest,
            "summary": self.summary,
            "warnings": self.warnings,
        }


def read_shading_file(path: str) -> ShadingTable:
    """Read and parse one shading file (blocking; run in the executor)."""
    if not os.path.isfile(path):
        if os.path.exists(path):
            raise ShadingError(f"'{path}' is not a regular file")
        raise ShadingError(f"file '{path}' not found")
    try:
        stat = os.stat(path)
    except OSError as err:
        raise ShadingError(f"cannot be read ({type(err).__name__})") from None
    if stat.st_size > SHADING_FILE_MAX_BYTES:
        raise ShadingError(f"file is {stat.st_size} bytes, limit {SHADING_FILE_MAX_BYTES}")
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(SHADING_FILE_MAX_BYTES + 1)
    except (OSError, UnicodeDecodeError) as err:
        raise ShadingError(f"cannot be read ({type(err).__name__})") from None
    table = load_shading_text(text, path)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[path] = (stat.st_size, stat.st_mtime_ns, table)
    return table


def validate_shading_file(path: str) -> str | None:
    """Config-flow helper (blocking): None when valid, else the message."""
    try:
        read_shading_file(path)
    except ShadingError as err:
        return str(err)
    except Exception as err:  # noqa: BLE001 - never let a flow crash
        return f"unexpected error ({type(err).__name__})"
    return None


def _issue_id(entry_id: str, index: int) -> str:
    return f"shading_{entry_id}_{index}"


def async_delete_shading_issues(hass: HomeAssistant, entry_id: str, keep: int = 0) -> None:
    """Delete this entry's shading repair issues for arrays >= ``keep``."""
    prefix = f"shading_{entry_id}_"
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain != DOMAIN or not issue_id.startswith(prefix):
            continue
        suffix = issue_id[len(prefix):]
        if not suffix.isdigit() or int(suffix) >= keep:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_load_shading(
    hass: HomeAssistant,
    entry_id: str,
    paths: list[str | None],
    tracking: list[str],
    azimuths: list[float],
    tilts: list[float],
) -> tuple[list[ShadingTable | None], list[ShadingStatus]]:
    """Load the shading of every array. Never raises."""
    async_delete_shading_issues(hass, entry_id, keep=len(paths))
    tables: list[ShadingTable | None] = []
    statuses: list[ShadingStatus] = []
    for index, raw_path in enumerate(paths):
        path = str(raw_path or "").strip() or None
        issue = _issue_id(entry_id, index)
        if path is None:
            tables.append(None)
            statuses.append(ShadingStatus(index, None, "none"))
            ir.async_delete_issue(hass, DOMAIN, issue)
            continue
        try:
            if tracking[index] != "none":
                raise ShadingError("shading is only supported for fixed (non-tracking) arrays")
            table = await hass.async_add_executor_job(read_shading_file, path)
        except Exception as err:  # noqa: BLE001 - a bad file must not stop the forecast
            message = str(err) if isinstance(err, ShadingError) else (
                f"unexpected error ({type(err).__name__})"
            )
            message = sanitize_error(message)
            LOGGER.error("Shading file for array %s ignored: %s", index + 1, message)
            ir.async_create_issue(
                hass, DOMAIN, issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="invalid_shading_file",
                translation_placeholders={"array": str(index + 1), "path": path, "error": message},
            )
            tables.append(None)
            statuses.append(ShadingStatus(index, path, "error", error=message))
            continue
        ir.async_delete_issue(hass, DOMAIN, issue)
        status = ShadingStatus(index, path, table.source, digest=table.digest, summary=dict(table.summary))
        if table.source == "geometry":
            geo_az = table.summary.get("array_azimuth")
            geo_tilt = table.summary.get("array_tilt")
            if geo_az is not None and abs(((geo_az - azimuths[index] + 180) % 360) - 180) > 3:
                status.warnings.append(
                    f"geometry azimuth {geo_az:g} differs from the configured {azimuths[index]:g}"
                )
            if geo_tilt is not None and abs(geo_tilt - tilts[index]) > 3:
                status.warnings.append(
                    f"geometry tilt {geo_tilt:g} differs from the configured {tilts[index]:g}"
                )
        tables.append(table)
        statuses.append(status)
    return tables, statuses


class ShadingMixin:
    """Applies per-array shading tables to each ``_request`` response.

    The library requests one response per array, in array order, inside
    ``estimate()``; the mixin counts the calls to know which array a
    response belongs to.
    """

    shading_tables: list[ShadingTable | None] | None = None
    shading_hass: HomeAssistant | None = None
    shading_stats: list[dict[str, Any] | None] | None = None
    _shading_index: int = 0

    async def estimate(self):  # type: ignore[override]
        self._shading_index = 0
        self.shading_stats = []
        return await super().estimate()  # type: ignore[misc]

    async def _apply_shading(self, payload: Any, params: dict[str, Any] | None) -> Any:
        index = self._shading_index
        self._shading_index += 1
        tables = self.shading_tables
        table = tables[index] if tables is not None and index < len(tables) else None
        if table is None or params is None or not isinstance(payload, dict):
            if self.shading_stats is not None:
                self.shading_stats.append(None)
            return payload
        try:
            tilt = float(params["tilt"])
            # the library sends the API azimuth (south = 0); compass = API + 180
            azimuth = (float(params["azimuth"]) + 180.0) % 360.0
            latitude = float(params["latitude"])
            longitude = float(params["longitude"])
        except (KeyError, TypeError, ValueError):
            if self.shading_stats is not None:
                self.shading_stats.append(None)
            return payload
        if tilt != tilt or azimuth != azimuth:  # tracking ("nan"): not shaded
            if self.shading_stats is not None:
                self.shading_stats.append(None)
            return payload
        if self.shading_hass is not None:
            stats = await self.shading_hass.async_add_executor_job(
                shade_payload, payload, table, latitude, longitude, tilt, azimuth
            )
        else:
            stats = shade_payload(payload, table, latitude, longitude, tilt, azimuth)
        if self.shading_stats is not None:
            self.shading_stats.append(stats.as_dict())
        return payload


class ShadedOpenMeteoSolarForecast(ShadingMixin, OpenMeteoSolarForecast):
    """Open-Meteo forecast with per-array shading.

    Only used when at least one array has a valid shading table; otherwise
    the coordinator keeps the unmodified library class (parity).
    """

    async def _request(self, uri: str, *, params: dict[str, Any] | None = None) -> Any:
        payload = await super()._request(uri, params=params)
        return await self._apply_shading(payload, params)
