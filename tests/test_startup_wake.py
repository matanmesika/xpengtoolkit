"""Cold-start and watchdog wakes must share one persisted cooldown."""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng" / "__init__.py"


class FakeHass:
    def __init__(self, running=False):
        self.is_running = running

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class FakeStore:
    def __init__(self, events):
        self.events = events

    async def async_save(self, payload):
        self.events.append(("save", payload["last_escalation"]))


def exercise_startup(*, old_wake, now=200_000, running=False, allow_wake=True):
    tree = ast.parse(ROOT.read_text(encoding="utf8"))
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "async_setup_entry")
    candidates = [n for n in ast.walk(setup) if isinstance(n, ast.If)
                  and ast.unparse(n.test) ==
                  "not hass.is_running and coordinator.command_policy.can_wake()"]
    assert len(candidates) == 1
    wrapper = ast.parse("async def boot():\n    pass\n").body[0]
    wrapper.body = [candidates[0]]
    module = ast.Module(body=[wrapper], type_ignores=[])
    events = []
    def send_command(*args):
        events.append(("wake", args))
        return True
    def send_query(*args):
        events.append(("passive", args))
        return True
    state = {"last_escalation": old_wake}
    timer = {"last_sent": 0, "last_transition_request": 0}
    env = {
        "hass": FakeHass(running),
        "coordinator": SimpleNamespace(
            command_policy=SimpleNamespace(can_wake=lambda: allow_wake)
        ),
        "watchdog_state": state, "watchdog_store": FakeStore(events),
        "query_timer_state": timer,
        "time": SimpleNamespace(time=lambda: now),
        "_LOGGER": logging.getLogger(__name__),
        "ESCALATE_BACKOFF_SECONDS": 43_200,
        "mqtt_client": SimpleNamespace(
            send_command=send_command, send_query=send_query),
        "vin": "TESTVIN",
    }
    exec(compile(ast.fix_missing_locations(module), str(ROOT), "exec"), env)
    asyncio.run(env["boot"]())
    return events, state, timer


def test_first_cold_start_wakes_but_persists_before_send():
    events, state, timer = exercise_startup(old_wake=0)
    assert [x[0] for x in events] == ["save", "wake"]
    assert state["last_escalation"] == 200_000
    assert timer["last_transition_request"] == 200_000


@pytest.mark.parametrize("previous", [200_000 - 1, 200_000 - 60, 200_000 - 43_199])
def test_reboot_inside_12_hours_never_wakes(previous):
    events, state, _ = exercise_startup(old_wake=previous)
    assert [x[0] for x in events] == ["passive"]
    assert state["last_escalation"] == previous


def test_reboot_after_cooldown_can_wake_again():
    events, state, _ = exercise_startup(old_wake=200_000 - 43_200)
    assert [x[0] for x in events] == ["save", "wake"]
    assert state["last_escalation"] == 200_000


def test_wake_permission_disabled_is_passive():
    events, state, _ = exercise_startup(old_wake=0, allow_wake=False)
    assert [x[0] for x in events] == ["passive"]
    assert state["last_escalation"] == 0


def test_reload_during_ha_runtime_is_passive():
    events, _, _ = exercise_startup(old_wake=0, running=True)
    assert [x[0] for x in events] == ["passive"]


def test_watchdog_uses_same_persisted_store_and_budget():
    source = ROOT.read_text(encoding="utf8")
    assert source.count("watchdog_store: Store = Store(") == 1
    assert source.index("watchdog_store: Store = Store(") < source.index("# 5. Initial query.")
    assert 'await watchdog_store.async_save({"last_escalation": now_ts})' in source
    assert source.count('await watchdog_store.async_save({"last_escalation": now_ts})') == 2
