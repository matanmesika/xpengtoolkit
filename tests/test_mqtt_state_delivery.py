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
_spec = importlib.util.spec_from_file_location("mqtt_client_delivery", MODULE_PATH)
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules["mqtt_client_delivery"] = mqtt_client
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


class RealChangesStillArrive(unittest.TestCase):
    def test_drive_frame_carries_privacy_safe_raw_diagnostics(self):
        client, calls = _client()
        message = _Msg(_state(
            instant={"drive": {"shift_state": 2, "speed": -4}},
            interval={"drive": {
                "speed": -4, "latitude": 32.1, "longitude": 34.2,
                "coordValid": 1,
            }},
        ))
        message.retain = True

        client._on_message(None, None, message)

        self.assertEqual(len(calls), 1)
        diagnostics = calls[0]["mqtt_diagnostics"]
        self.assertTrue(diagnostics["retained"])
        self.assertEqual(diagnostics["raw_shift_state"], 2)
        self.assertTrue(diagnostics["raw_shift_present"])
        self.assertEqual(diagnostics["raw_instant_speed"], -4)
        self.assertEqual(diagnostics["raw_interval_speed"], -4)
        self.assertTrue(diagnostics["raw_gps_present"])
        self.assertEqual(diagnostics["raw_coord_valid"], 1)
        self.assertEqual(diagnostics["shape"], "partial")
        self.assertGreater(diagnostics["payload_bytes"], 0)
        self.assertNotIn("latitude", diagnostics)
        self.assertNotIn("longitude", diagnostics)

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


class ARefusedStateIsNotData(unittest.TestCase):
    """The coordinator can refuse a state as a replay of an older snapshot.

    Measured 2026-10-21: a wedged fanout replayed the pre-drive snapshot for
    2.5 h. If a refused state still stamped ``_last_meaningful_state_ts``, the
    watchdog's 30-minute escalation would never fire and the fanout would stay
    wedged - the same silent failure as the asleep-ack bug above, from the other
    direction. A refused state must also not become the baseline that later
    partials merge into, or the stale snapshot keeps resurrecting itself.
    """

    FULL = _state(
        instant={
            "ext": {"vehicleMode": 1},
            "charge": {"battery_soc": 52},
            "odometer": {"total_mileage": 62210},
        }
    )

    def _client_with_verdict(self, verdict):
        client, calls = _client()
        client.set_state_callback(lambda data: (calls.append(data), verdict)[1])
        return client, calls

    def test_a_refused_state_does_not_stamp_the_clock(self):
        client, calls = self._client_with_verdict(False)
        before = client.last_meaningful_state_ts
        client._on_message(None, None, _Msg(self.FULL))
        self.assertEqual(len(calls), 1, "the coordinator must be asked")
        self.assertEqual(
            client.last_meaningful_state_ts, before,
            "a refused replay is not data and must age towards the escalation",
        )

    def test_a_refused_state_does_not_become_the_partial_baseline(self):
        client, _calls = self._client_with_verdict(False)
        client._on_message(None, None, _Msg(self.FULL))
        self.assertEqual(client._last_full_content, CACHED)

    def test_an_accepted_state_stamps_and_becomes_the_baseline(self):
        client, calls = self._client_with_verdict(True)
        before = client.last_meaningful_state_ts
        client._on_message(None, None, _Msg(self.FULL))
        self.assertEqual(len(calls), 1)
        self.assertGreater(client.last_meaningful_state_ts, before)
        self.assertEqual(
            client._last_full_content["instant"]["odometer"]["total_mileage"], 62210
        )

    def test_a_callback_that_returns_nothing_still_counts_as_data(self):
        """Backwards compatibility: callbacks predating the verdict return None."""
        client, calls = _client()
        client._on_message(
            None, None, _Msg(_state(instant={"charge": {"battery_soc": 63}}))
        )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
