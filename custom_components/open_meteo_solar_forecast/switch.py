"""Switches (0.1.34): only the learning hub has any."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .learning_entities import ApplyLearningSwitch, FreezeLearningSwitch, SameDaySwitch
from .learning_hub import LearningHub
from .runtime import SolarForecastConfigEntry, is_hub_entry

# Owner actions change the shared learning state: one at a time (the hub
# additionally serialises them with its runs).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarForecastConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if not is_hub_entry(entry) or not isinstance(entry.runtime_data, LearningHub):
        return
    hub = entry.runtime_data
    async_add_entities([ApplyLearningSwitch(hub), FreezeLearningSwitch(hub), SameDaySwitch(hub)])
