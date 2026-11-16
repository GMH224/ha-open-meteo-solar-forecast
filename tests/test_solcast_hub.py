"""Solcast as a second light forecast (0.1.37) in Home Assistant: the hub's
own calls (schedule, budget, errors), the combination in the members'
forecast (shadow, check, switch), learning records, nightly learning,
diagnostics and export without the key, reset."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import solcast as SC
from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.open_meteo_solar_forecast.learning_hub import get_hub

from .fusion_fixtures import LAT, LON
from .test_learning_hub import NOW, basic, refresh, today_kwh
from .test_solcast import answer

UTC = timezone.utc
URL = re.compile(r"^https://api\.solcast\.com\.au/rooftop_sites/abcd-1234-ef56-7890/forecasts")
KEY = "very-secret-solcast-key"
SITE = "abcd-1234-ef56-7890"
SUNRISE = L.sunrise_utc("2026-10-07", __import__("zoneinfo").ZoneInfo("Europe/Zurich"), LAT, LON)
SLOT = SUNRISE + timedelta(minutes=41)  # inside the sunrise + 40 slot
DECISION = SUNRISE + timedelta(hours=1, minutes=2)
HOUR = SLOT.replace(minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


async def configured(hass, freezer, aioclient_mock, *, p=0.05, status=200, limit=10):
    """A hub with a Solcast site; Solcast answers a dark sky (p kW of 1 kWp)."""
    fake, array, hub_entry, _ = await basic(hass, freezer)
    hass.config_entries.async_update_entry(
        hub_entry, options={**hub_entry.options, "solcast_api_key": KEY, "solcast_site_id": SITE,
                            "solcast_api_limit": limit})
    await hass.async_block_till_done(wait_background_tasks=True)
    hub = get_hub(hass)
    hub.data.d["last_nightly"] = NOW.isoformat()  # no catch-up nightly run in the middle of a test
    aioclient_mock.get(URL, json=answer(NOW, p=p, spread=0.02), status=status)
    return fake, array, hub_entry, hub


async def call_at(hass, freezer, hub, moment):
    freezer.move_to(moment)
    async with hub._lock:
        result = await hub._async_solcast_call(moment)
    await hass.async_block_till_done(wait_background_tasks=True)
    return result


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


async def test_not_configured_never_calls(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await basic(hass, freezer)
    assert hub.solcast_options is None and hub.solcast_state == "not_configured"
    assert await call_at(hass, freezer, hub, SLOT) == "not configured"
    assert aioclient_mock.call_count == 0


async def test_a_slot_calls_once_with_the_key_and_stores_the_answer(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await configured(hass, freezer, aioclient_mock)
    assert await call_at(hass, freezer, hub, SLOT - timedelta(minutes=5)) == "nothing due"
    result = await call_at(hass, freezer, hub, SLOT)
    assert result.startswith("sunrise+40:") and aioclient_mock.call_count == 1
    method, url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {KEY}" and url.query["format"] == "json"
    section = hub.data.d["solcast"]
    assert SC.calls_today(section, SLOT) == 1 and len(section["issues"]) == 1
    assert section["failures"] == 0 and section["last_success"]
    assert await call_at(hass, freezer, hub, SLOT + timedelta(minutes=5)) == "nothing due"
    assert aioclient_mock.call_count == 1


async def test_budget_is_respected_across_a_restart(hass, zurich, freezer, aioclient_mock):
    _, _, hub_entry, hub = await configured(hass, freezer, aioclient_mock, limit=3)
    hub.data.d["solcast"]["calls"][SC.utc_day(SLOT)] = 2  # planned budget for a limit of 3: 2
    hub._save()
    await hass.config_entries.async_reload(hub_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    hub = get_hub(hass)
    assert await call_at(hass, freezer, hub, SLOT) == "nothing due"
    assert aioclient_mock.call_count == 0


async def test_busy_solcast_gets_one_retry(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await configured(hass, freezer, aioclient_mock, status=429)
    result = await call_at(hass, freezer, hub, SLOT)
    assert "HTTP 429" in result
    section = hub.data.d["solcast"]
    assert section["failures"] == 1 and section["retry_at"] is not None
    retry = datetime.fromtimestamp(section["retry_at"], tz=UTC)
    assert SLOT + timedelta(minutes=5) <= retry <= SLOT + timedelta(minutes=15)
    assert "retry" in await call_at(hass, freezer, hub, retry)
    assert section["retry_at"] is None and section["failures"] == 2  # no second retry
    assert SC.calls_today(section, retry) == 2


async def test_a_rejected_key_is_not_retried_and_warns_after_three(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await configured(hass, freezer, aioclient_mock, status=401)
    assert "API key rejected" in await call_at(hass, freezer, hub, SLOT)
    section = hub.data.d["solcast"]
    assert section["retry_at"] is None and KEY not in section["last_error"]
    section["failures"] = 2
    hub.data.d["solcast"]["slots"].clear()
    await call_at(hass, freezer, hub, SLOT + timedelta(minutes=1))
    assert any(e[1] == "solcast_failing" for e in hub.data.d["events"])


async def test_network_errors_and_bad_answers_never_break_the_run(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await configured(hass, freezer, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, exc=aiohttp.ClientConnectionError("down"))
    assert "ClientConnectionError" in await call_at(hass, freezer, hub, SLOT)
    assert hub.data.d["solcast"]["retry_at"] is not None
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json={"forecasts": "nothing"})
    hub.data.d["solcast"]["retry_at"] = None
    hub.data.d["solcast"]["slots"].clear()
    assert "answer not usable" in await call_at(hass, freezer, hub, SLOT + timedelta(minutes=1))
    assert hub.data.d["solcast"]["issues"] == {}


# ---------------------------------------------------------------------------
# The combination in the members' forecast
# ---------------------------------------------------------------------------


async def with_weights(hass, freezer, aioclient_mock, weights=(0.8, 0.8, 0.8, 0.8, 0.8)):
    fake, array, hub_entry, hub = await configured(hass, freezer, aioclient_mock, p=0.05)
    await call_at(hass, freezer, hub, SLOT)
    hub.data.d["solcast"]["blend"] = {"c": 1150.0, "weights": list(weights), "computed": "test"}
    return fake, array, hub_entry, hub


async def test_combination_in_shadow_until_check_and_switch(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock)
    fake.publish(HOUR)
    before = today_kwh(hass)
    report = (await refresh(hass, array)).learning_report["solcast"]
    assert report["hours_changed"] > 0 and not report["served"]
    assert report["today_with_wh"] < report["today_without_wh"]  # Solcast forecasts a dark day
    assert today_kwh(hass) == before  # shadow: nothing served
    await hub.async_set_solcast(True)
    await refresh(hass, array)
    assert today_kwh(hass) == before and not hub.blend_applies  # switch on, check not passed
    hub.data.d["solcast"]["gate"] = {"passed": True, "days": 20, "reasons": []}
    assert hub.blend_applies and hub.solcast_state == "applied"
    report = (await refresh(hass, array)).learning_report["solcast"]
    assert report["served"] and today_kwh(hass) < before
    await hub.async_set_solcast(False)
    await refresh(hass, array)
    assert today_kwh(hass) == before and hub.solcast_state == "eligible"


async def test_weight_zero_or_no_answer_changes_nothing(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock, weights=(0.0,) * 5)
    hub.data.d["solcast"]["gate"] = {"passed": True, "days": 20, "reasons": []}
    await hub.async_set_solcast(True)
    fake.publish(HOUR)
    before = today_kwh(hass)
    report = (await refresh(hass, array)).learning_report["solcast"]
    assert report["hours_changed"] == 0 and report["today_with_wh"] == report["today_without_wh"]
    assert today_kwh(hass) == before
    hub.data.d["solcast"]["blend"]["weights"] = [0.8] * 5
    hub.data.d["solcast"]["issues"].clear()  # no Solcast answer: ours alone
    report = (await refresh(hass, array)).learning_report["solcast"]
    assert report["hours_changed"] == 0 and today_kwh(hass) == before


async def test_a_failing_combination_leaves_the_forecast(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock)
    hub.data.d["solcast"]["gate"] = {"passed": True, "days": 20, "reasons": []}
    await hub.async_set_solcast(True)
    fake.publish(HOUR)

    async def broken(*_a, **_k):
        raise RuntimeError("boom")

    with patch.object(type(hub), "async_blend_plan", broken):
        coordinator = await refresh(hass, array)
    assert coordinator.learning_report["solcast"]["served"] is False
    assert "RuntimeError" in coordinator.learning_report["solcast"]["error"]


async def test_pairs_and_the_decision_are_recorded(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock)
    fake.publish(HOUR)
    await refresh(hass, array)  # before sunrise + 1 h: pairs only
    section = hub.data.d["solcast"]
    assert section["pairs"] and "2026-10-07" not in section["decisions"]
    freezer.move_to(DECISION)
    fake.publish(DECISION.replace(minute=0, second=0, microsecond=0))
    await refresh(hass, array)
    record = section["decisions"]["2026-10-07"]
    assert record["captured"] == int(DECISION.timestamp())
    assert any(row[1] is not None for row in record["hours"].values())
    ours, blended = record["entries"][array.entry_id]
    assert blended < ours


async def test_nightly_learns_and_checks(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock)
    fake.publish(HOUR)
    await refresh(hass, array)
    night = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)
    freezer.move_to(night)
    result = await hub._async_solcast_nightly(night)
    section = hub.data.d["solcast"]
    assert section["blend"]["c"] == SC.C_PRIOR and section["blend"]["weights"] == [0.0] * 5  # no station hours
    assert section["gate"]["passed"] is False and section["gate_history"]
    assert "check not passed" in result
    assert hass.states.get("sensor.solar_forecast_learning_solcast_blend").state == "collecting"


# ---------------------------------------------------------------------------
# Entities, diagnostics, export, reset
# ---------------------------------------------------------------------------


async def test_entities(hass, zurich, freezer, aioclient_mock):
    _, _, _, hub = await with_weights(hass, freezer, aioclient_mock)
    hub._notify()
    await hass.async_block_till_done()
    state = hass.states.get("sensor.solar_forecast_learning_solcast_blend")
    assert state.state == "collecting"
    assert state.attributes["weights"] == [0.8] * 5 and state.attributes["calls_today"] == 1
    assert KEY not in json.dumps(dict(state.attributes)) and SITE not in json.dumps(dict(state.attributes))
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": "switch.solar_forecast_learning_solcast_switch"}, blocking=True)
    assert hub.data.solcast_switch


async def test_diagnostics_and_export_never_contain_the_key(hass, zurich, freezer, aioclient_mock, tmp_path):
    fake, array, hub_entry, hub = await with_weights(hass, freezer, aioclient_mock)
    fake.publish(HOUR)
    await refresh(hass, array)
    await hass.async_block_till_done(wait_background_tasks=True)
    diag = await async_get_config_entry_diagnostics(hass, hub_entry)
    text = json.dumps(diag, default=str)
    assert KEY not in text and SITE not in text
    assert (diag.get("data") or diag)["solcast"]["configured"] is True
    with patch.object(type(hub), "export_dir", lambda self: tmp_path):
        await hub.async_export(SLOT)
    files = {p.name: p.read_text() for p in tmp_path.iterdir() if p.is_file()}
    for name in ("solcast_pairs.csv", "solcast_issues.csv", "solcast_decision_hours.csv",
                 "solcast_decision_energy.csv"):
        assert name in files
    assert all(KEY not in body and SITE not in body for body in files.values())


async def test_full_reset_keeps_budget_and_answers(hass, zurich, freezer, aioclient_mock):
    fake, array, _, hub = await with_weights(hass, freezer, aioclient_mock)
    fake.publish(HOUR)
    await refresh(hass, array)
    await hub.async_set_solcast(True)
    hub.data.d["solcast"]["gate"] = {"passed": True, "days": 20, "reasons": []}
    calls = dict(hub.data.d["solcast"]["calls"])
    issues = dict(hub.data.d["solcast"]["issues"])
    await hub.async_reset("all")
    section = hub.data.d["solcast"]
    assert section["calls"] == calls and section["issues"] == issues
    assert section["pairs"] == {} and section["blend"] is None and section["gate"] is None
    assert not hub.data.solcast_switch
