#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delivery in both directions.

    python3 tests/test_mqtt_state_delivery.py
    python3 -m unittest discover -s tests -v

**Car → Home Assistant: an asleep car's ack is not data.** A parked XPENG answers
a query with a short ack - 244 B on a G6, measured 2026-10-16 - whose ``instant``
is empty and whose ``interval`` block repeats what is already held. Deciding that
by shape (the old test only caught a message with *no* ``interval`` at all) let
this ack through as a merged partial, and merging re-stamps
``_last_meaningful_state_ts``. That is the clock the silence watchdog escalates
on, so the one deliberate force-upload that wakes a parked car never fired and
every sensor sat at its last value for hours while the diagnostics reported data
seconds old.

The rule is now "does the candidate state differ from the one already held", which
cannot drop a real change - including a value going *to* zero.

**Home Assistant → car: a command that was never published must not report
success.** With the MQTT client reconnecting, the publish is dropped
(``Cannot send command: not connected``) while the service returns normally and
the lock entity flips to its optimistic state, so the UI says "locked" for a car
that was never asked.

Loaded by path with a `paho` stub, as `mqtt_client` imports paho (not installed
on this host) but no Home Assistant.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

MODULE_PATH = _component.module_path("mqtt_client.py")
COMPONENT = _component.component_dir()
VIN = "TESTVIN0000000000"
USER_ID = "12345678"


def _stub_paho() -> None:
    """Provide a permissive `paho.mqtt.client` if paho is absent."""
    try:
        import paho.mqtt.client  # noqa: F401

        return
    except ImportError:
        pass

    class _Any(types.ModuleType):
        def __getattr__(self, name):
            child = _Any(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    paho = _Any("paho")
    mqtt = _Any("paho.mqtt")
    client = _Any("paho.mqtt.client")
    mqtt.client = client
    paho.mqtt = mqtt
    sys.modules.setdefault("paho", paho)
    sys.modules.setdefault("paho.mqtt", mqtt)
    sys.modules.setdefault("paho.mqtt.client", client)


_stub_paho()
# The production module uses a relative import (.location_freshness).
# Load it inside a minimal fake package so these tests remain standalone
# without importing the Home Assistant integration package.
_PACKAGE = "xpeng_mqtt_state_delivery_testpkg"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(COMPONENT)]
sys.modules.setdefault(_PACKAGE, _package)
_MODULE_NAME = f"{_PACKAGE}.mqtt_client_delivery"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, MODULE_PATH)
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = mqtt_client
_spec.loader.exec_module(mqtt_client)


class _Msg:
    """The little of a paho message the handler touches."""

    def __init__(self, body: dict) -> None:
        self.topic = f"Xmart/Msg/User/{USER_ID}"
        self.payload = json.dumps(body).encode("utf-8")


def _state(**content) -> dict:
    """A ``service_type 19`` state response carrying ``content`` as msg_content."""
    return {
        "service_type": 19,
        "msg_type": 1,
        "target_id": VIN,
        "msg_content": {"code": 200, **content},
    }


#: What the integration already holds: a full state with charge and interval data.
CACHED = {
    "code": 200,
    "instant": {"charge": {"battery_soc": 64, "charge_power": 0}},
    "interval": {"charge": {"power": 0, "electricity": 0}},
}


def _client(cached: dict | None = None):
    client = mqtt_client.XPengMQTTClient(
        client_id=f"xmart:app:{USER_ID}:abcdef",
        password="P1",
        server="iot-app-global.xiaopeng.com:8885",
        user_id=USER_ID,
    )
    calls: list = []
    client.set_state_callback(calls.append)
    client._last_full_content = CACHED if cached is None else cached
    return client, calls


class AsleepAckIsNotData(unittest.TestCase):
    def test_the_asleep_ack_does_not_reach_the_coordinator(self):
        """The 244 B ack: empty ``instant``, an ``interval`` we already hold."""
        client, calls = _client()
        before = client.last_meaningful_state_ts
        client._on_message(None, None, _Msg(_state(interval=CACHED["interval"])))
        self.assertEqual(calls, [])
        self.assertEqual(client.last_meaningful_state_ts, before)

    def test_a_repeated_partial_is_not_data_either(self):
        """Same section values again: nothing changed, so nothing to deliver."""
        client, calls = _client()
        client._on_message(
            None, None, _Msg(_state(instant={"charge": dict(CACHED["instant"]["charge"])}))
        )
        self.assertEqual(calls, [])

    def test_an_ack_with_nothing_but_a_code_is_not_data(self):
        client, calls = _client()
        client._on_message(None, None, _Msg(_state()))
        self.assertEqual(calls, [])

    def test_repeated_changed_partial_does_not_mask_staleness(self):
        """A partial changed since FULL must be accepted only once.

        Without advancing the comparison baseline, every identical retry
        fires the coordinator callback and resets the watchdog's meaningful
        data timer, even though there is no further change.
        """
        client, calls = _client()
        first = {"charge": {"power": 1200, "electricity": 1}}
        client._on_message(None, None, _Msg(_state(interval=first)))
        self.assertEqual(len(calls), 1)
        accepted_at = client.last_meaningful_state_ts
        self.assertEqual(client._last_full_content["interval"], first)

        # A repeated stub after that update must not appear as fresh data.
        client._on_message(None, None, _Msg(_state(interval=first)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.last_meaningful_state_ts, accepted_at)

        # New telemetry must still flow, including a value returning to zero.
        second = {"charge": {"power": 0, "electricity": 1}}
        client._on_message(None, None, _Msg(_state(interval=second)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["msg_content"]["interval"], second)


class RealChangesStillArrive(unittest.TestCase):
    def test_a_changed_section_reaches_the_coordinator(self):
        client, calls = _client()
        before = client.last_meaningful_state_ts
        client._on_message(
            None, None, _Msg(_state(instant={"charge": {"battery_soc": 63}}))
        )
        self.assertEqual(len(calls), 1)
        self.assertGreater(client.last_meaningful_state_ts, before)
        merged = calls[0]["msg_content"]
        self.assertEqual(merged["instant"]["charge"]["battery_soc"], 63)
        # The rest of the last full state is still there.
        self.assertEqual(merged["interval"], CACHED["interval"])

    def test_a_changed_interval_reaches_the_coordinator(self):
        """Energy data arrives on its own, with an empty ``instant``."""
        client, calls = _client()
        client._on_message(
            None, None, _Msg(_state(interval={"charge": {"power": 5200, "electricity": 3}}))
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["msg_content"]["interval"]["charge"]["power"], 5200)

    def test_a_value_going_to_zero_is_still_a_change(self):
        """The reason the rule compares states instead of hunting for zeros."""
        cached = {
            "code": 200,
            "instant": {"charge": {"charge_power": 5200}},
            "interval": {"charge": {"power": 5200}},
        }
        client, calls = _client(cached)
        client._on_message(
            None, None, _Msg(_state(interval={"charge": {"power": 0}}))
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["msg_content"]["interval"]["charge"]["power"], 0)


class NarrowPartialMergeKeepsVehicleFields(unittest.TestCase):
    def test_speed_patch_preserves_last_gps_and_other_sections(self):
        """A speed-only interval patch must not erase GPS or battery values."""
        cached = {
            "code": 200,
            "instant": {
                "drive": {"shift_state": 1, "motor_temperature": 33},
                "charge": {"battery_soc": 50, "charging_state": 0},
            },
            "interval": {
                "drive": {"latitude": 32.12, "longitude": 34.82, "speed": 35},
                "charge": {"power": 0, "electricity": 2},
            },
        }
        client, calls = _client(cached)
        client._on_message(None, None, _Msg(_state(
            instant={"drive": {"shift_state": 3}},
            interval={"drive": {"speed": 0}},
        )))
        self.assertEqual(len(calls), 1)
        merged = calls[0]["msg_content"]
        self.assertEqual(merged["interval"]["drive"]["speed"], 0)
        self.assertEqual(merged["interval"]["drive"]["latitude"], 32.12)
        self.assertEqual(merged["interval"]["drive"]["longitude"], 34.82)
        self.assertEqual(merged["interval"]["charge"], cached["interval"]["charge"])
        self.assertEqual(merged["instant"]["drive"]["shift_state"], 3)
        self.assertEqual(merged["instant"]["drive"]["motor_temperature"], 33)
        self.assertEqual(merged["instant"]["charge"]["battery_soc"], 50)
        self.assertEqual(cached["instant"]["drive"]["shift_state"], 1)
        self.assertEqual(cached["interval"]["drive"]["speed"], 35)

        # Same partial a second time must not reset the data freshness clock.
        last = client.last_meaningful_state_ts
        client._on_message(None, None, _Msg(_state(
            instant={"drive": {"shift_state": 3}},
            interval={"drive": {"speed": 0}},
        )))
        self.assertEqual(len(calls), 1)
        self.assertEqual(last, client.last_meaningful_state_ts)

    def test_nested_charge_update_preserves_other_fields(self):
        """A SOC patch must not make the charge limit disappear."""
        cached = {"code": 200, "instant": {"charge": {
            "battery_soc": 55, "charge_limit": 80, "charging_state": 2
        }}}
        client, calls = _client(cached)
        client._on_message(None, None, _Msg(_state(
            instant={"charge": {"battery_soc": 56}}
        )))
        self.assertEqual(len(calls), 1)
        updated = calls[-1]["msg_content"]["instant"]["charge"]
        self.assertEqual(updated["battery_soc"], 56)
        self.assertEqual(updated["charge_limit"], 80)
        self.assertEqual(updated["charging_state"], 2)


class UnsentCommandsAreReported(unittest.TestCase):
    """A command that never went out must raise, not return."""

    def test_the_services_raise(self):
        source = (COMPONENT / "services.py").read_text(encoding="utf-8")
        self.assertIn("if not mqtt_client.send_command(", source)
        self.assertIn("raise HomeAssistantError(COMMAND_NOT_SENT_MESSAGE)", source)

    def test_the_buttons_raise(self):
        source = (COMPONENT / "button.py").read_text(encoding="utf-8")
        self.assertIn("if not sent:", source)
        self.assertIn("raise HomeAssistantError(COMMAND_NOT_SENT_MESSAGE)", source)

    def test_the_lock_raises(self):
        source = (COMPONENT / "lock.py").read_text(encoding="utf-8")
        self.assertIn("if not sent:", source)
        self.assertIn("raise HomeAssistantError(COMMAND_NOT_SENT_MESSAGE)", source)

    def test_the_message_is_defined_once(self):
        for name in ("services.py", "button.py", "lock.py"):
            text = (COMPONENT / name).read_text(encoding="utf-8")
            self.assertIn("COMMAND_NOT_SENT_MESSAGE", text)
            self.assertNotIn(
                "Command not sent:", text, f"{name} should use the shared constant"
            )
        self.assertIn("Command not sent:", (COMPONENT / "const.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
