"""Learning hub tests that need the recorder (0.1.34).

Separate module: the in-memory recorder must be set up before Home
Assistant, so no autouse fixture here may depend on ``hass``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_components.open_meteo_solar_forecast import learning as L

from .fusion_fixtures import LAT, LON
from .test_learning_hub import ENERGY, HOUR0, NOW, UTC, basic


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


async def _import_production(hass, entity, start, hours, kwh_per_hour):
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    total = 1000.0
    stats = []
    for h in range(hours):
        total += kwh_per_hour(start + timedelta(hours=h))
        stats.append({"start": start + timedelta(hours=h), "sum": total, "state": total})
    async_import_statistics(
        hass,
        {"mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None, "source": "recorder",
         "statistic_id": entity, "unit_class": "energy", "unit_of_measurement": "kWh"},
        stats,
    )
    await async_wait_recording_done(hass)


async def test_nightly_run_reads_production_models_decides_and_exports(recorder_mock, enable_custom_integrations, hass, zurich, freezer):
    fake, array, _, hub = await basic(hass, freezer)
    # Pretend the store was created two days ago, with station data for yesterday.
    hub.data.d["created"] = (NOW - timedelta(days=2)).isoformat()
    yesterday = (NOW.astimezone(hub.tz).date() - timedelta(days=1)).isoformat()
    for key in L.local_day_hours(yesterday, hub.tz):
        e0h, elev = L.hour_geometry(key, LAT, LON)
        if elev >= L.MIN_ELEVATION_DEG:
            ghi = 0.5 * e0h
            hub.data.d["station_hours"][key] = [ghi, 0.4 * ghi, 20.0, 330.0, 0.1 * ghi, 10.0, 6.0]

    def produced(moment):
        meas = hub.data.d["station_hours"].get(L.hour_key(moment))
        return 0.0 if meas is None else meas[0] * 6.0 * 0.8 / 1000.0  # ~ 6 kWp

    start = datetime.fromisoformat(yesterday).replace(tzinfo=hub.tz).astimezone(UTC) - timedelta(days=1)
    await _import_production(hass, ENERGY, start, 72, produced)
    await hub.async_run_nightly(NOW)
    assert hub.last_nightly["production"].startswith("10K:")
    production = hub.data.d["production"]["10K"]
    assert len(production) >= 24
    model = hub.data.d["model"]["10K"]
    assert model and all(v >= 0 for v in model.values())
    decision = hub.data.d["days"][yesterday]
    assert decision["status"] in (L.DAY_OK, "not_representative")
    assert "ratio" in decision["details"]
    assert hub.data.d["gate"]["days"] == 0 and not hub.data.d["gate"]["passed"]
    assert hub.state == L.STATE_COLLECTING
    assert hub.data.d["last_nightly"] == NOW.isoformat()
    assert (Path(hass.config.path("open_meteo_solar_forecast", "export")) / "days.csv").exists()
    assert "10K" in hub.data.d["stage2"]["groups"]
    # only days since the store was created are decided
    assert set(hub.data.d["days"]) <= {"2026-10-05", "2026-10-06"}


async def test_station_backfill_from_history(recorder_mock, enable_custom_integrations, hass, zurich, freezer):
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    fake, _, _, hub = await basic(hass, freezer)
    for minutes in range(10, 70, 10):
        freezer.move_to(HOUR0 - timedelta(hours=2) + timedelta(minutes=minutes + 12))
        fake.station(HOUR0 - timedelta(hours=2) + timedelta(minutes=minutes), ghi=50.0)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(NOW)
    hub.data.d["station_samples"].clear()
    hub.data.d["station_hours"].clear()
    note = await hub._async_backfill_station(NOW, timedelta(hours=6))
    assert note.startswith("6 samples")
    assert L.hour_key(HOUR0 - timedelta(hours=2)) in hub.data.d["station_hours"]




async def test_backfill_reads_recent_values_relative_to_the_state_time(
    recorder_mock, enable_custom_integrations, hass, zurich, freezer
):
    """A state written 9 h ago carries its own list: judged against its own
    time, not against now (otherwise the first-start backfill loses it)."""
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    fake, _, _, hub = await basic(hass, freezer)
    hour = HOUR0 - timedelta(hours=10)
    ends = [hour + timedelta(minutes=10 * i) for i in range(1, 7)]
    freezer.move_to(ends[-1] + timedelta(minutes=15))
    recent = [{"time": e.isoformat(), "values": fake.station_values(200.0)} for e in ends]
    fake.station(ends[-1], ghi=200.0, recent=recent)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(NOW)
    hub.data.d["station_samples"].clear()
    hub.data.d["station_hours"].clear()
    await hub._async_backfill_station(NOW, timedelta(hours=12))
    assert hub.data.d["station_hours"][L.hour_key(hour)][-1] == 6.0
