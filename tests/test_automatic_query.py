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


async def simulate_transition(*, previous, current, has_new_motion=True,
                              may_wake=True, published=True):
    """Execute the ACTUAL nested handlers with small HA/MQTT substitutes."""
    tree = ast.parse(SRC.read_text(encoding="utf8"))
    names = ("_request_transition_full_state", "_handle_vehicle_state")
    functions = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names
    ]
    assert len(functions) == 2
    body = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *functions
    ], type_ignores=[])
    for node in functions:
        node.decorator_list = []

    jobs = []
    tasks = []
    statuses = []
    class Hass:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)
        def async_create_task(self, coro):
            tasks.append(asyncio.create_task(coro))
    class Coord:
        vehicle_parked = previous
        command_policy = SimpleNamespace(can_wake=lambda: may_wake)
        def set_vehicle_data(self, data):
            self.vehicle_parked = data["current"]
            data.pop("has_fresh_shift_data", None)
            data.pop("has_fresh_speed_data", None)
        def set_poll_status(self, **kwargs):
            statuses.append(kwargs)
    def publish(*args):
        jobs.append(args)
        return published

    timer = {"last_sent": 0, "last_transition_request": 0}
    env = {
        "Any": object, "time": time, "vin": "TESTVIN",
        "_LOGGER": logging.getLogger(__name__),
        "query_timer_state": timer,
        "coordinator": Coord(),
        "mqtt_client": SimpleNamespace(send_command=publish),
        "hass": Hass(),
    }
    exec(compile(ast.fix_missing_locations(body), str(SRC), "exec"), env)
    env["_handle_vehicle_state"]({
        "current": current,
        "has_fresh_shift_data": has_new_motion,
        "has_fresh_speed_data": False,
    })
    if tasks:
        await asyncio.gather(*tasks)
    return jobs, timer, statuses, env


@pytest.mark.parametrize("previous,current,fresh,allowed,expected", [
    (True, False, True, True, 1),   # P -> D
    (None, False, True, True, 1),   # unknown after sleep -> D or R
    (False, False, True, True, 0),  # normal stream: do not force again
    (True, True, True, True, 0),   # stays parked
    (None, False, False, True, 0), # cached/REST data cannot trigger wake
    (True, False, True, False, 0), # explicit permission denial respected
])
def test_immediate_full_snapshot_only_on_confirmed_drive_transition(
    previous, current, fresh, allowed, expected
):
    jobs, timer, statuses, _ = asyncio.run(simulate_transition(
        previous=previous, current=current,
        has_new_motion=fresh, may_wake=allowed
    ))
    assert len(jobs) == expected
    if expected:
        assert jobs[0][1:] == (19, {"cmd_type": 2}, 2)
        assert timer["last_sent"] > 0
        assert statuses[-1]["last_query_kind"] == "transition_force_upload"
    else:
        assert timer["last_sent"] == 0


def test_transition_failed_publish_is_reported_without_fake_success():
    jobs, timer, statuses, _ = asyncio.run(simulate_transition(
        previous=None, current=False, published=False
    ))
    assert len(jobs) == 1
    assert timer["last_sent"] == 0
    assert statuses[-1].get("last_query_failed_at", 0) > 0
    assert "last_query_at" not in statuses[-1]


def test_transition_force_request_is_throttled_between_gear_toggles():
    async def run_two():
        jobs, timer, _, env = await simulate_transition(previous=True, current=False)
        assert len(jobs) == 1
        env["coordinator"].vehicle_parked = True
        env["_handle_vehicle_state"]({
            "current": False,
            "has_fresh_shift_data": True,
        })
        await asyncio.sleep(0)
        assert len(jobs) == 1
        assert timer["last_transition_request"] > 0
    asyncio.run(run_two())
