"""Regression coverage for stale/restored XPENG GPS and MQTT partial frames.

The production freshness helper, MQTT message handler, coordinator state setter,
and device tracker class are executed via AST extraction so this standalone suite
doesn't require a Home Assistant installation or credentials.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import time

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"
FRESHNESS = ROOT / "location_freshness.py"


@pytest.fixture(scope="module")
def freshness():
    spec = importlib.util.spec_from_file_location("xpeng_location_freshness_test", FRESHNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("content", [
    None,
    {},
    {"interval": {}},
    {"interval": {"drive": {}}},
    {"interval": {"drive": {"latitude": 0, "longitude": 34.8}}},
    {"interval": {"drive": {"latitude": 32, "longitude": "nan"}}},
    {"interval": {"drive": {"latitude": 92, "longitude": 34.8}}},
    {"interval": {"drive": {"latitude": 32}}},
    {"interval": {"drive": {"latitude": 32, "longitude": []}}},
])
def test_invalid_or_missing_gps_cannot_refresh_location(freshness, content):
    assert not freshness.has_fresh_position(content)


def test_only_real_coordinates_are_fresh(freshness):
    content = {"interval": {"drive": {"latitude": 32.123, "longitude": 34.834}}}
    assert freshness.has_fresh_position(content)


def test_expiry_and_restore_policy(freshness):
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert freshness.location_is_fresh(now - timedelta(seconds=899), now)
    assert not freshness.location_is_fresh(now - timedelta(seconds=900), now)
    assert not freshness.location_is_fresh(now - timedelta(seconds=901), now)
    assert not freshness.location_is_fresh(now, now, restored=True)
    assert not freshness.location_is_fresh(None, now)
    assert not freshness.location_is_fresh(now + timedelta(seconds=10), now)


def extracted_function(file, method_name, *, class_name=None, namespace=None):
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    nodes = tree.body
    if class_name:
        cls = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name)
        nodes = cls.body
    method = next(
        n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == method_name
    )
    # Class instance methods have no decorators; no runtime HA imports needed.
    method.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method],
        type_ignores=[],
    )
    env = namespace or {}
    exec(compile(ast.fix_missing_locations(module), str(ROOT / file), "exec"), env)
    return env[method_name]


def make_client(callbacks, freshness):
    return SimpleNamespace(
        recv_topic="Xmart/Msg/User/1234",
        _last_state_msg_ts=0,
        _last_msg_ts=0,
        _last_meaningful_state_ts=0,
        _last_full_content=None,
        _state_callback=callbacks.append,
        _resolve_pending=lambda *_: None,
        _handle_listener_message=lambda *_args, **_kwargs: None,
        _fire_notification=lambda *_: None,
        _fire_command_feedback=lambda *_: None,
    )


def send(handler, client, payload):
    handler(
        client, None, None,
        SimpleNamespace(topic=client.recv_topic, payload=json.dumps(payload).encode(), qos=1),
    )


def frame(content):
    return {"target_id": "REDACTED", "service_type": 19, "msg_type": 1, "msg_content": content}


def test_real_mqtt_full_frame_marks_location_fresh(freshness):
    cb = []
    client = make_client(cb, freshness)
    method = extracted_function(
        "mqtt_client.py", "_on_message", class_name="XPengMQTTClient",
        namespace={"json": json, "time": time, "_LOGGER": logging.getLogger(__name__),
                   "has_fresh_position": freshness.has_fresh_position},
    )
    content = {
        "code": 200,
        "instant": {"ext": {"mode": 1}, "charge": {"battery_soc": 50}},
        "interval": {"drive": {"latitude": 32.1, "longitude": 34.9}},
    }
    send(method, client, frame(content))
    assert len(cb) == 1
    assert cb[0]["has_fresh_location_data"] is True
    assert cb[0]["has_fresh_energy_data"] is True


def test_partial_without_gps_does_not_refresh_old_merged_fix(freshness):
    cb = []
    client = make_client(cb, freshness)
    method = extracted_function(
        "mqtt_client.py", "_on_message", class_name="XPengMQTTClient",
        namespace={"json": json, "time": time, "_LOGGER": logging.getLogger(__name__),
                   "has_fresh_position": freshness.has_fresh_position},
    )
    content = {
        "code": 200,
        "instant": {"ext": {"mode": 1}, "charge": {"battery_soc": 50}},
        "interval": {"drive": {"latitude": 32.1, "longitude": 34.9}},
    }
    send(method, client, frame(content))
    assert cb[-1]["has_fresh_location_data"] is True
    send(method, client, frame({"code": 200, "instant": {"door": {"left": 1}}}))
    assert len(cb) == 2
    assert cb[-1]["msg_content"]["interval"]["drive"]["latitude"] == 32.1
    assert cb[-1]["has_fresh_location_data"] is False


def test_duplicate_stub_is_not_new_fix(freshness):
    cb = []
    client = make_client(cb, freshness)
    method = extracted_function(
        "mqtt_client.py", "_on_message", class_name="XPengMQTTClient",
        namespace={"json": json, "time": time, "_LOGGER": logging.getLogger(__name__),
                   "has_fresh_position": freshness.has_fresh_position},
    )
    base = {
        "code": 200,
        "instant": {"ext": {"mode": 1}, "charge": {"battery_soc": 50}},
        "interval": {"drive": {"latitude": 32.1, "longitude": 34.9}},
    }
    send(method, client, frame(base))
    send(method, client, frame({"code": 200, "instant": {}, "interval": base["interval"]}))
    assert len(cb) == 1


def test_coordinator_stamps_only_explicit_location_frames():
    method = extracted_function(
        "coordinator.py", "set_vehicle_data",
        class_name="XPengDataCoordinator",
        namespace={"datetime": datetime, "timezone": timezone, "_LOGGER": logging.getLogger(__name__)},
    )
    updates = []
    coord = SimpleNamespace(
        last_location_timestamp=None,
        last_data_timestamp=None,
        last_energy_data_timestamp=None,
        state_is_restored=True,
        _update_parked_consumption=lambda _: None,
        async_set_updated_data=updates.append,
        _persist_state=lambda: None,
    )
    method(coord, {"msg_content": {"interval": {"drive": {"latitude": 32.1}}},
                   "has_fresh_location_data": True})
    first_fix = coord.last_location_timestamp
    assert isinstance(first_fix, datetime)
    assert not coord.state_is_restored
    method(coord, {"msg_content": {"instant": {"door": {"left": 1}}},
                   "has_fresh_location_data": False})
    assert coord.last_location_timestamp == first_fix
    assert len(updates) == 2
    assert "has_fresh_location_data" not in updates[-1]


def test_tracker_availability_ages_out_without_new_push(freshness):
    class FakeBase:
        def __init__(self, coordinator):
            self.coordinator = coordinator
            self._vin = "REDACTED"
            self.hass = "fake-hass"
            self.remove_callbacks = []
            self.writes = 0

        @property
        def available(self):
            return True

        async def async_added_to_hass(self):
            self.was_added = True

        def async_on_remove(self, cb):
            self.remove_callbacks.append(cb)

        def async_write_ha_state(self):
            self.writes += 1

        def _get_nested_value(self, name):
            return None

    class FakeTracker:
        pass

    scheduled = []
    def fake_timer(hass, callback, interval):
        scheduled.append((callback, interval))
        return lambda: None

    tree = ast.parse((ROOT / "device_tracker.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "XPengDeviceTracker")
    cls.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls],
        type_ignores=[],
    )
    env = {
        "XPengEntity": FakeBase, "TrackerEntity": FakeTracker,
        "location_is_fresh": freshness.location_is_fresh,
        "location_age_seconds": freshness.location_age_seconds,
        "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
        "async_track_time_interval": fake_timer,
    }
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "device_tracker.py"), "exec"), env)
    cls = env["XPengDeviceTracker"]

    now = datetime.now(timezone.utc)
    coord = SimpleNamespace(last_location_timestamp=now - timedelta(seconds=30), state_is_restored=False)
    entity = cls(coord)
    assert entity.available
    coord.last_location_timestamp = now - timedelta(seconds=1000)
    assert not entity.available
    coord.last_location_timestamp = now
    coord.state_is_restored = True
    assert not entity.available

    import asyncio
    asyncio.run(entity.async_added_to_hass())
    assert len(scheduled) == 1
    assert scheduled[0][1] == timedelta(seconds=60)
    scheduled[0][0](datetime.now(timezone.utc))
    assert entity.writes == 1
    assert len(entity.remove_callbacks) == 1
