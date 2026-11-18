"""0.1.37.2: the nightly timer ran on a worker thread since 0.1.36.0 (its
`@callback` marker was lost when the house-light timer was added) and failed
under Python 3.14 ("loop is not the running loop"). These tests check that
every timer starts its job on the event loop, and that the hourly run catches
up an overdue nightly run.
"""

from __future__ import annotations

import inspect
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import is_callback
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.open_meteo_solar_forecast import learning_hub as LH
from custom_components.open_meteo_solar_forecast.learning_hub import LearningHub

from .fusion_fixtures import LAT, LON
from .test_learning_hub import basic

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


TIMER_CALLBACKS = [name for name, _ in inspect.getmembers(LearningHub, inspect.isfunction) if name.endswith("_cb")]


def test_every_timer_callback_is_marked():
    assert {"_hourly_cb", "_same_day_cb", "_house_ghi_cb", "_nightly_cb", "_startup_cb"} <= set(TIMER_CALLBACKS)
    for name in TIMER_CALLBACKS:
        assert is_callback(getattr(LearningHub, name)), f"{name} lacks @callback: Home Assistant would run it in a thread"


@pytest.mark.parametrize(
    ("moment", "job"),
    [
        (datetime(2026, 10, 8, 1, 30, tzinfo=UTC), "nightly"),  # 03:30 local
        (datetime(2026, 10, 7, 5, 25, tzinfo=UTC), "hourly"),
        (datetime(2026, 10, 7, 5, 10, 40, tzinfo=UTC), "same-day"),
        (datetime(2026, 10, 7, 5, 17, tzinfo=UTC), "house light"),
    ],
)
async def test_timers_start_their_job_on_the_event_loop(hass, zurich, freezer, moment, job):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    loop_thread = threading.current_thread()
    started: list[tuple[str, bool]] = []
    original = hub.entry.async_create_background_task

    def record(hass_, target, name, *args, **kwargs):
        started.append((name, threading.current_thread() is loop_thread))
        return original(hass_, target, name, *args, **kwargs)

    with patch.object(hub.entry, "async_create_background_task", side_effect=record), \
            patch.object(hub, "async_run_nightly", AsyncMock()), \
            patch.object(hub, "async_run_hourly", AsyncMock()), \
            patch.object(hub, "async_run_same_day", AsyncMock()), \
            patch.object(hub, "async_run_house_ghi", AsyncMock()):
        freezer.move_to(moment)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    mine = [ok for name, ok in started if job in name]
    assert mine, f"the {job} timer did not fire: {started}"
    assert all(mine), f"the {job} timer started its job from a worker thread"


async def test_nightly_timer_runs_the_nightly_job(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    moment = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hub.data.d["last_nightly"] == moment.isoformat()
    assert hub.last_nightly["started"] == moment.isoformat()


# -- self-healing: the hourly run catches up an overdue nightly run ----------------------------


async def test_overdue_nightly_is_caught_up_by_the_hourly_run(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    now = datetime(2026, 10, 8, 10, 25, tzinfo=UTC)
    hub.data.d["last_nightly"] = (now - LH.NIGHTLY_OVERDUE - timedelta(minutes=1)).isoformat()
    freezer.move_to(now)
    await hub.async_run_hourly_timer(now)
    assert hub.data.d["last_nightly"] == now.isoformat()
    events = [e for e in hub.data.d["events"] if e[1] == "nightly_overdue"]
    assert len(events) == 1 and "caught up" in events[0][2]
    # the next hour: no longer overdue, nothing more
    later = now + timedelta(hours=1)
    with patch.object(hub, "async_run_nightly", AsyncMock()) as nightly:
        await hub.async_run_hourly_timer(later)
        nightly.assert_not_awaited()


async def test_nightly_not_overdue_is_left_to_its_timer(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    now = datetime(2026, 10, 8, 10, 25, tzinfo=UTC)
    hub.data.d["last_nightly"] = (now - LH.NIGHTLY_OVERDUE + timedelta(minutes=1)).isoformat()
    with patch.object(hub, "async_run_nightly", AsyncMock()) as nightly:
        await hub.async_run_hourly_timer(now)
        nightly.assert_not_awaited()
    assert not [e for e in hub.data.d["events"] if e[1] == "nightly_overdue"]


async def test_overdue_event_once_per_episode_while_the_catch_up_keeps_failing(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    now = datetime(2026, 10, 9, 10, 25, tzinfo=UTC)
    hub.data.d["last_nightly"] = (now - timedelta(days=3)).isoformat()
    with patch.object(hub, "async_run_nightly", AsyncMock()) as nightly:  # does not finish a run
        for hour in range(3):
            await hub.async_run_hourly_timer(now + timedelta(hours=hour))
        assert nightly.await_count == 3  # retried every hour, never more often
    assert len([e for e in hub.data.d["events"] if e[1] == "nightly_overdue"]) == 1
    # recovered, then overdue again: a new episode gets a new event
    hub.data.d["last_nightly"] = (now + timedelta(hours=3)).isoformat()
    await hub.async_run_hourly_timer(now + timedelta(hours=4))
    with patch.object(hub, "async_run_nightly", AsyncMock()):
        await hub.async_run_hourly_timer(now + timedelta(days=3))
    assert len([e for e in hub.data.d["events"] if e[1] == "nightly_overdue"]) == 2


async def test_store_without_nightly_counts_from_its_creation(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    now = datetime(2026, 10, 8, 10, 25, tzinfo=UTC)
    hub.data.d["last_nightly"] = None
    hub.data.d["created"] = (now - timedelta(hours=2)).isoformat()
    assert hub.nightly_overdue(now) is False
    hub.data.d["created"] = (now - timedelta(hours=27)).isoformat()
    assert hub.nightly_overdue(now) is True
    hub.data.d["created"] = None
    assert hub.nightly_overdue(now) is False


async def test_hourly_timer_uses_the_catch_up(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)
    moment = datetime(2026, 10, 9, 5, 25, tzinfo=UTC)
    freezer.move_to(moment - timedelta(minutes=2))  # let the regular timers of the past days fire first
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    hub.data.d["last_nightly"] = (moment - timedelta(hours=30)).isoformat()
    with patch.object(hub, "async_run_same_day", AsyncMock()), patch.object(hub, "async_run_house_ghi", AsyncMock()):
        freezer.move_to(moment)  # only the hourly timer is due now
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hub.data.d["last_nightly"] == moment.isoformat()
    assert [e for e in hub.data.d["events"] if e[1] == "nightly_overdue"]
