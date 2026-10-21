"""State-machine and diagnostic sensor regressions without a running car/HA."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import importlib.util
import logging
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"


@pytest.fixture(scope="module")
def ops():
    path = ROOT / "operational_state.py"
    spec = importlib.util.spec_from_file_location("xpeng_operational_state_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("connected,restored,age,parked,charging,cloud,expected", [
    (False, False, 2, False, False, 1, "Disconnected"),
    (True, True, 2, False, False, 1, "Cached"),
    (True, False, 400, False, False, 1, "Silent"),
    (True, False, None, None, False, None, "Silent"),
    (True, False, 2, False, False, 1, "Driving"),
    (True, False, 2, True, True, 1, "Charging"),
    (True, False, 2, True, False, 1, "Parked"),
    (True, False, 2, None, False, 1, "Standby"),
    (True, False, 2, None, False, 0, "Reported parked"),
    (True, False, 2, None, False, 2, "Reported driving"),
    (True, False, 2, None, False, None, "Unknown"),
])
def test_distinct_cloud_sleep_and_motion_states(
    ops, connected, restored, age, parked, charging, cloud, expected
):
    assert ops.classify(
        connected=connected, restored=restored, data_age=age,
        parked=parked, charging=charging, raw_mode=cloud,
    ) == expected


def extracted_class(name, *, ops):
    source = ROOT / "sensor.py"
    tree = ast.parse(source.read_text(encoding="utf8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    class Base:
        def __init__(self, coordinator):
            self.coordinator = coordinator
            self._vin = "TESTVIN"
    env = {
        "XPengPolledSensor": Base,
        "XPengDataCoordinator": object,
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
        "datetime": datetime, "timezone": timezone,
        "telemetry_age": ops.age_seconds,
        "classify_operational": ops.classify,
        "explain_operational": ops.explain,
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), env)
    return env[name]


def fake_coordinator(*, seconds_ago=4):
    recent = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    frame = {"msg_content": {"instant": {
        "ext": {"vehicleMode": 1}, "drive": {"shift_state": 1},
    }, "interval": {"drive": {"latitude": 32.1, "longitude": 34.8, "speed": 55}}}}
    return SimpleNamespace(
        last_full_data=frame,
        last_data_timestamp=recent, last_mode_timestamp=recent,
        last_shift_timestamp=recent, last_speed_timestamp=recent,
        last_location_timestamp=recent, last_charge_timestamp=recent,
        last_motion_transition="Parked -> Driving",
        last_motion_transition_timestamp=recent, motion_transition_count=1,
        vehicle_parked=False, is_charging=False,
        state_is_restored=False,
        poll_status={"mqtt_connected": True},
    )


def test_operational_state_sensor_shows_conflicting_raw_standby_and_driving(ops):
    cls = extracted_class("XPengOperationalStateSensor", ops=ops)
    coo = fake_coordinator()
    entity = cls(coo)
    assert entity.native_value == "Driving"
    attrs = entity.extra_state_attributes
    assert attrs["raw_cloud_vehicle_mode"] == 1
    assert attrs["sleep_confirmed"] is False
    assert attrs["motion_transition_count"] == 1
    assert attrs["gps_age_s"] is not None
    coo.last_data_timestamp -= timedelta(minutes=6)
    assert entity.native_value == "Silent"
    coo.poll_status["mqtt_connected"] = False
    assert entity.native_value == "Disconnected"


def test_motion_and_gps_diagnostics_freshness(ops):
    c = fake_coordinator()
    motion = extracted_class("XPengMotionDiagnosticSensor", ops=ops)(c)
    gps = extracted_class("XPengGpsFreshnessSensor", ops=ops)(c)
    assert motion.native_value == "Driving"
    assert motion.extra_state_attributes["transition_count"] == 1
    assert gps.native_value == "Fresh"
    c.last_location_timestamp -= timedelta(minutes=16)
    assert gps.native_value == "Stale"
    c.vehicle_parked = None
    assert motion.native_value == "Unknown"
    c.state_is_restored = True
    assert gps.native_value == "Cached"


def test_sensor_platform_registers_diagnostics():
    source = (ROOT / "sensor.py").read_text(encoding="utf8")
    for cls in ("XPengOperationalStateSensor", "XPengMotionDiagnosticSensor",
                "XPengGpsFreshnessSensor"):
        assert f"entities.append({cls}(coordinator))" in source


def test_transition_sequence_only_counts_real_gear_flips():
    tree = ast.parse((ROOT / "coordinator.py").read_text(encoding="utf8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "XPengDataCoordinator")
    names = ("vehicle_parked", "set_vehicle_data")
    code = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *[node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    ], type_ignores=[])
    # Remove @property because methods are compiled outside the coordinator.
    for node in code.body[1:]:
        node.decorator_list = []
    env = {"datetime": datetime, "timezone": timezone, "math": math, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(code), str(ROOT / "coordinator.py"), "exec"), env)

    class Coordinator:
        vehicle_parked = property(env["vehicle_parked"])
        def __init__(self):
            now = datetime.now(timezone.utc)
            self._last_full_data = {"msg_content": {"instant": {
                "drive": {"shift_state": 3}
            }, "interval": {"drive": {"speed": 0}}}}
            self.last_shift_timestamp = now
            self.last_speed_timestamp = now
            self.last_data_timestamp = now
            self.last_mode_timestamp = None
            self.last_location_timestamp = None
            self.last_energy_data_timestamp = None
            self.last_charge_timestamp = None
            self.state_is_restored = False
            self.motion_transition_count = 0
            self.last_motion_transition_timestamp = None
            self.last_motion_transition = None

        def _update_parked_consumption(self, *_): pass
        def async_set_updated_data(self, *_): pass
        def _persist_state(self): pass

    coo = Coordinator()
    def push(shift, speed):
        env["set_vehicle_data"](coo, {
            "msg_content": {"instant": {"drive": {"shift_state": shift}},
                            "interval": {"drive": {"speed": speed}}},
            "has_fresh_shift_data": True, "has_fresh_speed_data": True,
        })

    assert coo.vehicle_parked is True
    push(1, 20)
    assert coo.vehicle_parked is False
    assert coo.motion_transition_count == 1
    push(1, 30)
    assert coo.motion_transition_count == 1
    push(3, 0)
    assert coo.vehicle_parked is True
    assert coo.motion_transition_count == 2
    assert coo.last_motion_transition == "Driving -> Parked"
