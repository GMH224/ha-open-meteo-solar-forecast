"""Shared pytest configuration (0.1.33.2).

Runs against a real Home Assistant through
pytest-homeassistant-custom-component. Network sockets are disabled by that
plugin, which is relied upon: a test in local mode that accidentally reached
Open-Meteo would fail rather than pass silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]


def pytest_runtest_setup(item):
    """Restore an event loop before each test.

    tests/test_config_flow.py (pre-existing) uses
    unittest.IsolatedAsyncioTestCase, which leaves the thread without a
    current event loop; the Home Assistant fixtures that run afterwards in
    the same session then fail at setup (defect P-02 in the release audit).
    """
    import asyncio

    policy = asyncio.get_event_loop_policy()
    try:
        loop = policy.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        policy.set_event_loop(policy.new_event_loop())
