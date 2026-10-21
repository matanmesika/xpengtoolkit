"""Regression tests for XPENG motion detection and safe polling cadence.

Run without Home Assistant, devices or XPENG credentials.
"""
from __future__ import annotations

import ast
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"


def method(name, *, filename="coordinator.py", class_name="XPengDataCoordinator"):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == class_name)
    node = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        node,
    ], type_ignores=[])
    env = {"datetime": datetime, "timezone": timezone, "math": math,
           "_LOGGER": logging.getLogger(__name__)}
    if filename == "sensor.py":
        def age(ts, now):
            return max(0, int((now - ts).total_seconds())) if ts is not None else None
        env["telemetry_age"] = age
    exec(compile(ast.fix_missing_locations(module), str(ROOT / filename), "exec"), env)
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
    (None, -5, None, 5, False),  # signed reverse speed alone proves movement
    (3, -5, 20, 2, False),    # fresh reverse movement defeats stale P gear
    (3, -5, 2, 20, True),    # newer P wins over stale negative reverse speed
    (2, -5, 2, 20, False),    # current R also proves not parked
    (2, 0, 2, 20, False),     # R at zero speed is still an active drive gear
    (None, float("nan"), None, 2, None),  # bad input is not movement
    (None, float("-inf"), None, 2, None),  # bad input is not movement
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
        last_charge_timestamp=now - timedelta(seconds=60),
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



@pytest.mark.parametrize("seconds_ago,charging_state,power,expected", [
    (5, 2, 0, True),
    (5, 1, 2100, True),
    (601, 2, 0, False),
    (601, 1, 2100, False),
    (None, 2, 0, False),
])
def test_charging_cadence_expires_stale_charge_samples(
    seconds_ago, charging_state, power, expected
):
    now = datetime.now(timezone.utc)
    state = SimpleNamespace(
        _last_full_data={"msg_content": {
            "instant": {"charge": {"charging_state": charging_state}},
            "interval": {"charge": {"power": power}},
        }},
        last_charge_timestamp=(
            now - timedelta(seconds=seconds_ago)
            if seconds_ago is not None else None
        ),
    )
    assert method("is_charging")(state) is expected


def test_charge_timestamp_is_only_refreshed_by_charge_telemetry():
    setter = method("set_vehicle_data")
    old = datetime.now(timezone.utc) - timedelta(seconds=400)
    values = []
    state = SimpleNamespace(
        last_data_timestamp=None, last_energy_data_timestamp=None,
        last_location_timestamp=None, last_shift_timestamp=None,
        last_speed_timestamp=None, last_charge_timestamp=old,
        state_is_restored=False,
        _update_parked_consumption=lambda data: None,
        async_set_updated_data=values.append,
        _persist_state=lambda: None,
    )
    setter(state, {"msg_content": {"instant": {"door": {"left": 1}}}})
    assert state.last_charge_timestamp == old
    setter(state, {"msg_content": {"interval": {"charge": {"power": 0}}},
                   "has_fresh_charge_data": True})
    assert state.last_charge_timestamp > old


def test_raw_mode_remains_raw_while_speed_and_gps_expire():
    native_value = method("native_value", filename="sensor.py",
                          class_name="XPengSensor")
    now = datetime.now(timezone.utc)
    def reading(key, parked, raw, *, age=2, restored=False, converter=lambda v: v):
        stamp = now - timedelta(seconds=age) if age is not None else None
        coord = SimpleNamespace(
            vehicle_parked=parked, last_mode_timestamp=stamp,
            last_speed_timestamp=stamp, last_shift_timestamp=stamp,
            last_location_timestamp=stamp, state_is_restored=restored,
        )
        obj = SimpleNamespace(
            coordinator=coord,
            entity_description=SimpleNamespace(
                key=key, value_path="any", value_fn=converter),
            _get_nested_value=lambda _: raw,
        )
        return native_value(obj)

    # Cloud Standby remains the raw observation. The derived diagnostic
    # (tested separately) should say Driving when recent motion proves it.
    assert reading("vehicle_mode", False, 1, converter=lambda _: "Standby") == "Standby"
    assert reading("speed", True, 117, converter=float) == 0.0
    assert reading("speed", False, 42, converter=float) == 42.0
    assert reading("speed", False, 42, age=601, converter=float) is None
    assert reading("shift_state", False, 1, age=601) is None
    assert reading("vehicle_mode", False, 1, age=901) is None
    assert reading("latitude", None, 32.1, age=901) is None
    assert reading("longitude", None, 34.8, age=3) == 34.8
    assert reading("latitude", None, 32.1, restored=True) is None
    assert reading("speed", None, None, converter=float) is None
