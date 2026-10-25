#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synthetic G6 telemetry regression: parked -> REST-only motion -> MQTT recovery.

Run without Home Assistant, vehicle credentials or a car:
    python -m unittest tests/test_stale_gps_motion_scenarios.py -v
    python -m unittest discover -s tests

Exercises the *real* MQTT parser, coordinator, drive evidence and watchdog policy
with fake HVAC, charge, TPMS, gear and GPS sensor messages. No real VIN/GPS.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import types
from typing import Any
import time
import unittest
from datetime import datetime, timedelta, timezone

from test_energy_guard import _coordinator_with
from test_mqtt_state_delivery import _Msg, _state, mqtt_client
import _component


def _policy():
    path = _component.module_path("watchdog_policy.py")
    spec = importlib.util.spec_from_file_location("xpeng_synthetic_policy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy = _policy()

VIN = "TESTVIN0000000000"
BASE = {
    "vin": VIN,
    "msg_content": {
        "code": 200,
        "instant": {
            "ext": {"vehicleMode": 1},
            "drive": {"shift_state": 4, "speed": 0},
            "odometer": {"total_mileage": 62234},
            "charge": {"battery_soc": 30},
            "hvac": {"hvac_inner_temp_float": 23},
            "tpms": {"fl": 275},
        },
        "interval": {
            "drive": {
                "latitude": 32.123,
                "longitude": 34.456,
                "coordValid": 1,
                "speed": 0,
            }
        },
    },
}


class FakeG6:
    """Minimal production-class stack; mocked transport and simulated sensors."""

    def __init__(self):
        import copy

        self.coord = _coordinator_with(copy.deepcopy(BASE))
        self.coord.max_odometer = 62234
        self.coord.gps_fix.observe(self.coord._last_full_data, now=1000.0)
        self.client = mqtt_client.XPengMQTTClient(
            client_id="xmart:app:12345678:fake",
            password="not-a-real-password",
            server="127.0.0.1:8885",
            user_id="12345678",
        )
        self.client._last_full_content = copy.deepcopy(BASE["msg_content"])
        self.client.set_state_callback(self.coord.set_vehicle_data)
        self.coord.set_baseline_sink(self.client.adopt_state)

    def push(self, **payload):
        self.client._on_message(None, None, _Msg(_state(**payload)))

    def rest(self, **widget):
        self.coord.last_energy_data_timestamp = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        self.coord.last_data_timestamp = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        self.coord._merge_widget_state_if_mqtt_stale(widget)


class ProductionHACallbackSimulation(unittest.TestCase):
    """Execute the actual nested HA event callback, not a replica of its logic."""

    def setUp(self):
        self.sim = FakeG6()
        path = _component.component_dir() / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_handle_vehicle_state")
        self.sent = []

        class FakeHass:
            def __init__(self, sent):
                self.sent = sent

            def async_add_executor_job(self, fn, *args):
                self.sent.append((fn, args))

        self.hass = FakeHass(self.sent)
        self.env = {
            "coordinator": self.sim.coord,
            "mqtt_client": self.sim.client,
            "hass": self.hass,
            "vin": VIN,
            "Any": Any,
            "datetime": datetime,
            "timezone": timezone,
            "_log_drive_evidence": lambda before, after, age: None,
            "_LOGGER": types.SimpleNamespace(info=lambda *args: None),
        }
        exec(compile(ast.fix_missing_locations(
            ast.Module(body=[fn], type_ignores=[])), "__init__.py", "exec"
        ), self.env)
        self.handler = self.env["_handle_vehicle_state"]

    def frame(self, shift=None, speed=None, *, interval_speed=None):
        import copy
        data = copy.deepcopy(self.sim.coord._last_full_data)
        raw = {"instant": {}, "interval": {}}
        if shift is not None or speed is not None:
            raw["instant"] = {"shift_state": shift, "speed": speed}
            data["msg_content"]["instant"]["drive"].update(raw["instant"])
        if interval_speed is not None:
            raw["interval"] = {"speed": interval_speed}
            data["msg_content"]["interval"]["drive"]["speed"] = interval_speed
        data["fresh_drive"] = raw
        data["has_fresh_gps_data"] = False
        return data

    def assert_full_state_publish(self):
        self.assertEqual(len(self.sent), 1)
        fn, args = self.sent[0]
        self.assertEqual(fn, self.sim.client.send_command)
        self.assertEqual(args, (VIN, 19, {"cmd_type": 2}, 2))

    def dispatch_parsed_state(self, data):
        """Join the real MQTT parser to the production HA state callback.

        Older tests exercised these two boundaries separately: the parser fed
        the coordinator directly, while the HA callback received hand-built
        dictionaries. This adapter lets a synthetic broker frame pass through
        both production paths in one test without opening a socket or calling a
        vehicle service.
        """
        was_parked = self.sim.coord.vehicle_parked
        self.handler(data, was_parked)
        return True

    def test_fresh_d_schedules_one_full_upload_not_passive_query(self):
        self.handler(self.frame(shift="D", speed=18), True)
        self.assertFalse(self.sim.coord.vehicle_parked)
        self.assert_full_state_publish()
        self.handler(self.frame(shift="D", speed=20), False)
        self.assertEqual(len(self.sent), 1, "no repeated transition upload")
        self.handler(self.frame(shift="P", speed=0), False)
        self.assertTrue(self.sim.coord.vehicle_parked)
        self.assertEqual(len(self.sent), 1)

    def test_speed_only_overrides_cached_p_and_upload_expires(self):
        self.handler(self.frame(interval_speed=18), True)
        self.assertFalse(self.sim.coord.vehicle_parked)
        self.assert_full_state_publish()
        self.sim.coord.mqtt_motion_until = time.time() - 1
        self.assertTrue(self.sim.coord.vehicle_parked)
        self.assertEqual(len(self.sent), 1)

    def test_parsed_drive_partial_gps_and_park_flow(self):
        """Exercise MQTT decode/merge -> HA callback -> coordinator end to end."""
        sim = self.sim
        sim.client.set_state_callback(self.dispatch_parsed_state)

        # A fresh D frame must update the verdict and queue one force-upload.
        sim.push(instant={"drive": {"shift_state": "D", "speed": 18}})
        self.assertFalse(sim.coord.vehicle_parked)
        self.assert_full_state_publish()

        # An unrelated partial inherits D in the merged state but must not
        # refresh the raw-motion clock or schedule another transition upload.
        motion_until = sim.coord.mqtt_motion_until
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 25}})
        self.assertEqual(sim.coord.mqtt_motion_until, motion_until)
        self.assertEqual(len(self.sent), 1)

        # GPS is refreshed only when the incoming frame itself has coordinates.
        old_fix_time = sim.coord.gps_fix.fixed_at
        sim.push(interval={"drive": {
            "latitude": 32.19, "longitude": 34.49, "coordValid": 1,
        }})
        self.assertEqual(sim.coord.gps_fix.position, (32.19, 34.49))
        self.assertGreater(sim.coord.gps_fix.fixed_at, old_fix_time)

        # A fresh P frame ends the temporary motion override.
        sim.push(instant={"drive": {"shift_state": "P", "speed": 0}})
        self.assertTrue(sim.coord.vehicle_parked)
        self.assertEqual(sim.coord.last_motion_at, 0)

    def test_full_recovery_flow_rest_motion_stale_gps_then_live_fix(self):
        """Drive through REST fallback, watchdog policy and MQTT recovery.

        This covers the failure shape where HVAC/SOC keep arriving, the GPS
        fix ages independently, REST proves movement, and the bounded watchdog
        is allowed one recovery request. A fresh MQTT fix must clear the stale
        condition; a later P must restore parked behavior.
        """
        sim = self.sim
        sim.client.set_state_callback(self.dispatch_parsed_state)
        simulated_start = time.time()
        sim.coord.gps_fix.observe(
            sim.coord._last_full_data, now=simulated_start - 31 * 60
        )

        # REST sees distance advance while the accepted MQTT baseline still
        # says parked. That is independent motion evidence, not a fresh GPS fix.
        sim.rest(odometer={"total_mileage": 62301})
        motion_at = sim.coord.last_motion_at
        now = time.time()
        self.assertGreater(motion_at, 0)
        self.assertAlmostEqual(sim.coord.gps_fix.age_seconds(now), 31 * 60, delta=0.1)

        # Repeating unrelated state packets must not refresh GPS or erase the
        # REST motion evidence. This is the exact mixed-stream stall pattern.
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 24}})
        sim.push(instant={"charge": {"battery_soc": 29}})
        self.assertAlmostEqual(sim.coord.gps_fix.age_seconds(now), 31 * 60, delta=0.1)
        self.assertEqual(sim.coord.last_motion_at, motion_at)
        self.assertTrue(policy.should_escalate_stale_gps(
            sim.coord.gps_fix.age_seconds(now), 0, now, motion_at
        ))

        # Once the bounded recovery has been spent, the same evidence cannot
        # force another request immediately. The parked-car policy remains safe.
        self.assertFalse(policy.should_escalate_stale_gps(
            sim.coord.gps_fix.age_seconds(now + 1), now, now + 1, motion_at
        ))
        sim.push(instant={"drive": {"shift_state": "D", "speed": 12}})
        self.assertFalse(sim.coord.vehicle_parked)
        self.assert_full_state_publish()

        # A real coordinate-bearing MQTT frame is what resolves the GPS stall.
        sim.push(interval={"drive": {
            "latitude": 32.19, "longitude": 34.49, "coordValid": 1,
        }})
        self.assertEqual(sim.coord.gps_fix.position, (32.19, 34.49))
        self.assertLess(sim.coord.gps_fix.age_seconds(time.time()), 2)

        # Fresh P terminates the motion episode; a stale fix alone cannot wake
        # a genuinely parked car after its evidence expires.
        sim.push(instant={"drive": {"shift_state": "P", "speed": 0}})
        self.assertTrue(sim.coord.vehicle_parked)
        self.assertEqual(sim.coord.last_motion_at, 0)


class MotionEvidenceAndGpsAge(unittest.TestCase):
    def setUp(self):
        self.sim = FakeG6()

    def test_new_mqtt_d_and_interval_speed_enable_short_lived_active_poll(self):
        sim = self.sim
        self.assertFalse(sim.coord.mqtt_motion_is_recent)
        sim.push(instant={"drive": {"shift_state": "D", "speed": 0}})
        self.assertTrue(sim.coord.mqtt_motion_is_recent)
        sim.push(instant={"drive": {"shift_state": "P", "speed": 0}})
        self.assertFalse(sim.coord.mqtt_motion_is_recent)
        # Speed-only motion is useful even while the cached instant gear is P.
        sim.push(interval={"drive": {"speed": 14}})
        self.assertTrue(sim.coord.mqtt_motion_is_recent)
        # Unrelated partials do not extend the activity window.
        deadline = sim.coord.mqtt_motion_until
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 28}})
        self.assertEqual(sim.coord.mqtt_motion_until, deadline)
        sim.coord.mqtt_motion_until = time.time() - 1
        self.assertFalse(sim.coord.mqtt_motion_is_recent)
        # Once expired the car receives no unbounded "driving" allowance.
        sim.push(instant={"tpms": {"fl": 276}})
        self.assertFalse(sim.coord.mqtt_motion_is_recent)

    def test_transitions_wired_to_full_state_query_not_stub_only(self):
        source = (_component.component_dir() / "__init__.py").read_text("utf-8")
        self.assertIn("previous_mqtt_motion = coordinator.mqtt_motion_is_recent", source)
        self.assertIn("recent_mqtt_motion = coordinator.mqtt_motion_is_recent", source)
        self.assertIn("mqtt_client.send_command, vin, 19, {\"cmd_type\": 2}, 2", source)


    def test_parked_hvac_does_not_fake_a_new_gps_fix(self):
        sim = self.sim
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 24}})
        self.assertEqual(sim.coord.gps_fix.position, (32.123, 34.456))
        self.assertEqual(sim.coord.gps_fix.age_seconds(1600), 600)
        self.assertEqual(sim.coord.vehicle_parked, True)

    def test_pure_battery_update_does_not_make_cached_gear_or_gps_fresh(self):
        sim = self.sim
        sim.rest(odometer={"total_mileage": 62301})
        motion_at = sim.coord.last_motion_at
        self.assertGreater(motion_at, 0)
        sim.push(instant={"charge": {"battery_soc": 29}})
        self.assertEqual(sim.coord.last_motion_at, motion_at)
        self.assertEqual(sim.coord.gps_fix.age_seconds(1500), 500)

    def test_tpms_and_hvac_do_not_clear_rest_motion(self):
        sim = self.sim
        sim.rest(odometer={"total_mileage": 62301})
        motion_at = sim.coord.last_motion_at
        sim.push(instant={"tpms": {"fl": 273}})
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 22}})
        self.assertEqual(sim.coord.last_motion_at, motion_at)
        now = time.time()
        self.assertTrue(
            policy.should_escalate_moving(
                silence_seconds=1801,
                last_escalation=now - 3600,
                now=now,
                last_motion_at=motion_at,
            )
        )

    def test_real_d_gear_and_then_real_p_gear_update_motion(self):
        sim = self.sim
        sim.push(instant={"drive": {"shift_state": "D", "speed": 15}})
        self.assertFalse(sim.coord.vehicle_parked)
        self.assertGreater(sim.coord.last_motion_at, 0)
        sim.push(instant={"drive": {"shift_state": "P", "speed": 0}})
        self.assertTrue(sim.coord.vehicle_parked)
        self.assertEqual(sim.coord.last_motion_at, 0)

    def test_real_p_frame_can_end_rest_motion_without_fake_p_from_cached_data(self):
        sim = self.sim
        sim.rest(odometer={"total_mileage": 62301})
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 25}})
        self.assertGreater(sim.coord.last_motion_at, 0)
        sim.push(instant={"drive": {"shift_state": "P"}, "charge": {"battery_soc": 29}})
        self.assertEqual(sim.coord.last_motion_at, 0)

    def test_new_interval_speed_is_motion_even_if_cached_gear_is_p(self):
        sim = self.sim
        sim.push(interval={"drive": {"speed": 12}})
        self.assertFalse(sim.coord.vehicle_parked)
        self.assertGreater(sim.coord.last_motion_at, 0)
        earlier = sim.coord.last_motion_at
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 26}})
        self.assertEqual(earlier, sim.coord.last_motion_at)

    def test_invalid_gps_then_new_valid_fix(self):
        sim = self.sim
        sim.push(interval={"drive": {
            "latitude": 31.9, "longitude": 35.1, "coordValid": 0
        }})
        self.assertEqual(sim.coord.gps_fix.position, (32.123, 34.456))
        self.assertIs(sim.coord.gps_fix.coord_valid, False)
        self.assertEqual(sim.coord.gps_fix.age_seconds(1600), 600)
        sim.push(interval={"drive": {
            "latitude": 31.9, "longitude": 35.1, "coordValid": 1
        }})
        self.assertEqual(sim.coord.gps_fix.position, (31.9, 35.1))
        self.assertIs(sim.coord.gps_fix.coord_valid, True)
        self.assertGreater(sim.coord.gps_fix.fixed_at, 1000)

    def test_repeated_309_byte_stubs_do_not_reset_gps_age_or_motion(self):
        sim = self.sim
        sim.rest(odometer={"total_mileage": 62301})
        motion_at = sim.coord.last_motion_at
        stamped = sim.client.last_meaningful_state_ts
        for _ in range(5):
            sim.push()
        self.assertEqual(sim.client.last_meaningful_state_ts, stamped)
        self.assertEqual(sim.coord.last_motion_at, motion_at)
        self.assertEqual(sim.coord.gps_fix.age_seconds(1600), 600)

    def test_parked_vehicle_keeps_12_hour_safety_budget(self):
        sim = self.sim
        sim.push(instant={"charge": {"battery_soc": 29}})
        self.assertEqual(sim.coord.last_motion_at, 0)
        now = time.time()
        self.assertFalse(policy.should_escalate_moving(2000, now - 3600, now, 0))
        self.assertFalse(policy.should_escalate(2000, now - 3600, now))

    def test_rest_detects_motion_when_battery_mqtt_is_fresh_but_gps_is_stale(self):
        sim = self.sim
        sim.coord.last_energy_data_timestamp = datetime.now(timezone.utc)
        sim.coord.last_data_timestamp = datetime.now(timezone.utc)
        sim.coord._merge_widget_state_if_mqtt_stale(
            {"odometer": {"total_mileage": 62301}, "charge": {"battery_soc": 28}}
        )
        self.assertGreater(sim.coord.last_motion_at, 0)
        # The energy from MQTT remains authoritative.
        self.assertEqual(
            sim.coord._last_full_data["msg_content"]["instant"]["charge"]["battery_soc"], 30
        )
        # GPS is still old: only a real MQTT GPS frame can refresh it.
        self.assertEqual(sim.coord.gps_fix.age_seconds(1800), 800)

    def test_gps_stalls_while_tpms_hvac_battery_keep_arriving(self):
        sim = self.sim
        for soc, temp in ((29, 24), (28, 25), (27, 26)):
            sim.push(instant={"charge": {"battery_soc": soc}})
            sim.push(instant={"hvac": {"hvac_inner_temp_float": temp}})
        self.assertEqual(sim.coord.gps_fix.age_seconds(3000), 2000)
        self.assertEqual(sim.coord.vehicle_parked, True)
        sim.coord.last_energy_data_timestamp = datetime.now(timezone.utc)
        sim.coord._merge_widget_state_if_mqtt_stale(
            {"odometer": {"total_mileage": 62301}}
        )
        now = time.time()
        self.assertTrue(policy.should_escalate_stale_gps(
            sim.coord.gps_fix.age_seconds(now),
            last_escalation=now - 3600,
            now=now,
            last_motion_at=sim.coord.last_motion_at,
        ))

    def test_spontaneous_wake_drive_then_real_gps_clears_stale_location(self):
        sim = self.sim
        self.assertTrue(sim.coord.vehicle_parked)
        sim.push(instant={"drive": {"shift_state": "D", "speed": 32}})
        self.assertFalse(sim.coord.vehicle_parked)
        sim.push(interval={"drive": {"latitude": 32.19, "longitude": 34.49, "coordValid": 1}})
        self.assertEqual(sim.coord.gps_fix.position, (32.19, 34.49))
        self.assertGreater(sim.coord.gps_fix.fixed_at, 1000)
        sim.push(instant={"drive": {"shift_state": "P", "speed": 0}})
        self.assertTrue(sim.coord.vehicle_parked)
        self.assertEqual(sim.coord.last_motion_at, 0)

    def test_fake_motion_from_a_cached_p_does_not_trigger_gps_wake(self):
        sim = self.sim
        sim.push(instant={"hvac": {"hvac_inner_temp_float": 24}})
        now = time.time()
        self.assertEqual(sim.coord.last_motion_at, 0)
        self.assertFalse(policy.should_escalate_stale_gps(
            fix_age_seconds=10 * 3600,
            last_escalation=0,
            now=now,
            last_motion_at=sim.coord.last_motion_at,
        ))

    def test_motion_escalation_is_once_per_episode(self):
        sim = self.sim
        sim.rest(odometer={"total_mileage": 62301})
        motion_at = sim.coord.last_motion_at
        self.assertTrue(policy.should_escalate_moving(1801, 0, motion_at + 1, motion_at))
        self.assertFalse(policy.should_escalate_moving(3600, motion_at + 1, motion_at + 700, motion_at))


if __name__ == "__main__":
    unittest.main(verbosity=2)
