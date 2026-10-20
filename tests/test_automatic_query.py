"""Simulate Home Assistant's periodic MQTT query and published-status truth."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time
import logging

import pytest

SRC = Path(__file__).resolve().parents[1] / "custom_components/xpeng/__init__.py"


class FakeHass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def tick(*, parked, charging=False, hint=False, published=True):
    parsed = ast.parse(SRC.read_text(encoding="utf8"))
    target = next(n for n in ast.walk(parsed)
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "_send_periodic_query")
    target.decorator_list = []
    code = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        target,
    ], type_ignores=[])

    sent = []
    mqtt = SimpleNamespace(
        send_query=lambda vin: sent.append("passive") is None and published,
        send_command=lambda *_: sent.append("force") is None and published,
    )
    status = {}
    def set_status(**values):
        status.update(values)
    coordinator = SimpleNamespace(
        vehicle_parked=parked, is_charging=charging,
        external_charging_hint=hint, set_poll_status=set_status,
    )
    timer = {"last_sent": 0}
    env = {
        "Any": object, "time": time,
        "coordinator": coordinator,
        "query_timer_state": timer,
        "mqtt_client": mqtt,
        "vin": "TESTVIN",
        "scan_interval": 60,
        "parked_scan_interval": 3600,
        "poll_status": SimpleNamespace(query_interval_seconds=lambda _p, _c, a, b, h: a if (not _p or _c or h) else b),
        "hass": FakeHass(),
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(code), str(SRC), "exec"), env)
    asyncio.run(env["_send_periodic_query"](None))
    return sent, timer, status


@pytest.mark.parametrize("parked,charging,hint,expected", [
    (True, False, False, "passive"),
    (True, False, True, "passive"),  # charging hint is not permission to wake
    (False, False, False, "force"),
    (True, True, False, "force"),
])
def test_mode_picks_right_query_and_reports_actual_success(
    parked, charging, hint, expected
):
    sent, timer, status = tick(parked=parked, charging=charging, hint=hint)
    assert sent == [expected]
    assert timer["last_sent"] > 0
    assert status["last_query_kind"] == ("force_upload" if expected == "force" else "passive")
    assert status["last_query_at"] > 0


def test_failed_publish_does_not_advance_schedule_or_claim_data():
    sent, timer, status = tick(parked=False, published=False)
    assert sent == ["force"]
    assert timer["last_sent"] == 0
    assert status["last_query_failed_at"] > 0
    assert "last_query_at" not in status
    assert "last_query_kind" not in status
