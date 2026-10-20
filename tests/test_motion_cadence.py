"""Regression tests for XPENG motion detection and safe polling cadence.

Run without Home Assistant, devices or XPENG credentials.
"""
from __future__ import annotations

import ast
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"


def method(name):
    tree = ast.parse((ROOT / "coordinator.py").read_text(encoding="utf8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "XPengDataCoordinator")
    node = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        node,
    ], type_ignores=[])
    env = {"datetime": datetime, "timezone": timezone,
           "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "coordinator.py"), "exec"), env)
    return env[name]


def coordinator(shift=None, speed=None, *, shift_age=None, speed_age=None):
    now = datetime.now(timezone.utc)
    data = {"msg_content": {
        "instant": {"drive": {} if shift is None else {"shift_state": shift}},
        "interval": {"drive": {} if speed is None else {"speed": speed}},
    }}
    return SimpleNamespace(
        _last_full_data=data,
        last_shift_timestamp=(now - timedelta(seconds=shift_age)) if shift_age is not None else None,
        last_speed_timestamp=(now - timedelta(seconds=speed_age)) if speed_age is not None else None,
    )


@pytest.mark.parametrize("shift,speed,shift_age,speed_age,expected", [
    (3, 42, 2, 20, True),    # newly parked, cached previous speed
    (3, 42, 20, 2, False),   # new speed defeats cached parked gear
    (1, 0, 20, 2, False),    # still in D when stopping at a traffic light
    (2, 0, 2, 20, False),    # reverse
    ("P", 0, 2, 20, True),
    ("N", 0, 2, 20, None),  # neutral is not proof of driving
    (0, 0, 2, 20, None),
    (1, 55, 601, 601, None), # past a ten-minute silent interval, state unknown
    (3, 55, 601, 601, None),
    (None, 60, None, 5, False),  # speed-only frame, no shift yet
    (None, 0, None, 5, None),
])
def test_motion_state_uses_newest_fresh_signal(
    shift, speed, shift_age, speed_age, expected
):
    f = method("vehicle_parked")
    state = coordinator(shift, speed, shift_age=shift_age, speed_age=speed_age)
    assert f(state) is expected


def test_motion_source_timestamps_only_advance_for_original_fields():
    func = method("set_vehicle_data")
    now = datetime.now(timezone.utc)
    updates = []
    c = SimpleNamespace(
        last_data_timestamp=None, last_energy_data_timestamp=None,
        last_location_timestamp=None,
        last_shift_timestamp=now - timedelta(seconds=60),
        last_speed_timestamp=now - timedelta(seconds=60),
        state_is_restored=False,
        _update_parked_consumption=lambda _data: None,
        async_set_updated_data=updates.append,
        _persist_state=lambda: None,
    )
    old_shift = c.last_shift_timestamp
    old_speed = c.last_speed_timestamp
    func(c, {"msg_content": {"instant": {"door": {"left": 1}}},
             "has_fresh_shift_data": False, "has_fresh_speed_data": False})
    assert c.last_shift_timestamp == old_shift
    assert c.last_speed_timestamp == old_speed
    func(c, {"msg_content": {"interval": {"drive": {"speed": 0}}},
             "has_fresh_shift_data": False, "has_fresh_speed_data": True})
    assert c.last_shift_timestamp == old_shift
    assert c.last_speed_timestamp > old_speed
    assert len(updates) == 2
    assert "has_fresh_shift_data" not in updates[-1]
    assert "has_fresh_speed_data" not in updates[-1]
